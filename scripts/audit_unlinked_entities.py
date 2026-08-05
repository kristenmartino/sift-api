"""Audit articles.entities mentions against the dossier catalog.

Run from sift-api root:
    railway run ./.venv/bin/python3 scripts/audit_unlinked_entities.py
    railway run ./.venv/bin/python3 scripts/audit_unlinked_entities.py --min-count 5

What it does:
1. Reports the **extraction denominator first**. `articles.entities` defaults to
   `'[]'` (a JSON array) but the batch poller writes an object, so
   `jsonb_typeof(entities) = 'object'` is the only honest "was extracted" test.
   Every ranking below is stated against that subset, not against all articles.
   Below --min-extraction-rate the script refuses to rank at all: a demand
   signal drawn from a third of the corpus is not a demand signal.
2. Rolls up every extracted person + organization mention, with article counts
   and category spread.
3. Anti-joins those mentions against the **catalog** — the four profile tables.
   `catalog_status = none` (no row exists at all) is the primary signal, and
   the reason to anti-join the catalog rather than `articles.entity_links`:
   a mention missing from entity_links may mean "no dossier" OR "dossier
   exists but the LLM judged the reference indirect" (the suppression rules
   in services/entity_linker_llm.py deliberately drop state/party/collective
   references). Those two are not separable from the columns alone.
4. Reports the entity_links delta **separately**, for exact catalog matches
   only: linked_article_count / matched_article_count. A low ratio is a
   linker-tuning problem, not a coverage problem. It never enters the ranking.
5. Sweeps for bill mentions by regex. services/entity_extractor.py emits no
   bills at all, so bill demand is invisible to steps 2-4 and has to be
   measured separately. Those rows are labelled signal=regex_sweep.
6. Rolls up top search_queries.query_norm as a second, independent demand
   signal, anti-joined against the same catalog.

Outputs three CSVs for human review:
    unlinked_entity_suggestions.csv   — the ranking, with a blank
                                        proposed_dossier_type column to fill in
    unlinked_entities_by_category.csv — which of the 10 beats are starved
    unmatched_search_queries.csv      — searches with no catalog row

Read-only: never writes to the database.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
from collections import defaultdict

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings

# Mentions shorter than this are noise ("EU", "AP", initials). Matches the
# spirit of _MIN_KEY_LENGTH in services/entity_linker.py.
_MIN_MENTION_LENGTH = 4

# Bill surface forms the extractor never produces. Two shapes: a chamber
# designator plus number, and a title-cased "... Act".
_BILL_NUMBER_RE = re.compile(r"\b(?:H\.?\s?R\.?|S\.?)\s?(\d{1,5})\b")
_BILL_TITLE_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|of|and|for|the)){0,5}\s+Act)\b")


def _normalize(s: str) -> str:
    return s.strip().lower()


# Name particles that carry no identity: middle initials and generational
# suffixes. Without dropping these, "Donald Trump" fails to match a catalog
# row reading "Donald J. Trump" and is reported as a coverage gap that
# doesn't exist — the single most dangerous false positive this script can
# produce, since it lands at the top of the ranking.
_NAME_NOISE = frozenset({"jr", "sr", "ii", "iii", "iv", "the"})
_PUNCT_RE = re.compile(r"[^\w\s]")


def _tokens(s: str) -> frozenset[str]:
    words = _PUNCT_RE.sub(" ", s).lower().split()
    return frozenset(w for w in words if len(w) > 1 and w not in _NAME_NOISE)


class CatalogIndex:
    """Match a free-text mention to a catalog row.

    Four tiers, strongest first. `exact` means "this entity has a dossier";
    `substring` means "probably, needs a human"; `none` is the signal this
    script exists to produce.
    """

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self.rows = rows
        self.by_name: dict[str, tuple[str, str]] = {}
        self.by_tokens: dict[frozenset[str], tuple[str, str]] = {}
        self.token_rows: list[tuple[frozenset[str], str, str]] = []
        for etype, cid, name in rows:
            self.by_name.setdefault(_normalize(name), (etype, cid))
            tok = _tokens(name)
            if tok:
                self.by_tokens.setdefault(tok, (etype, cid))
                self.token_rows.append((tok, etype, cid))

    def match(self, mention: str) -> tuple[str | None, str | None, str]:
        mn = _normalize(mention)
        if not mn:
            return None, None, "none"

        # 1. Literal equality.
        hit = self.by_name.get(mn)
        if hit:
            return hit[0], hit[1], "exact"

        # 2. Same name modulo middle initials, suffixes and punctuation.
        mtok = _tokens(mention)
        if mtok:
            hit = self.by_tokens.get(mtok)
            if hit:
                return hit[0], hit[1], "exact"

        # 3. Substring either direction, longest wins.
        best: tuple[str, str] | None = None
        best_score = -1
        for etype, cid, name in self.rows:
            nn = _normalize(name)
            if nn in mn or mn in nn:
                score = min(len(mn), len(nn))
                if score > best_score:
                    best = (etype, cid)
                    best_score = score
        if best is not None:
            return best[0], best[1], "substring"

        # 4. Multi-word token subset ("Kamala Harris" ⊂ "Kamala D. Harris").
        # Requires >=2 tokens: a bare surname must not claim a full name.
        if len(mtok) >= 2:
            for tok, etype, cid in self.token_rows:
                if mtok < tok or tok < mtok:
                    return etype, cid, "substring"

        return None, None, "none"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_DENOMINATOR_SQL = """
SELECT COUNT(*)::int AS total,
       COUNT(*) FILTER (WHERE jsonb_typeof(entities) = 'object')::int AS extracted,
       COUNT(*) FILTER (WHERE jsonb_typeof(entities) = 'array')::int  AS never_extracted,
       COUNT(*) FILTER (WHERE COALESCE(jsonb_array_length(entity_links), 0) > 0)::int AS has_links
FROM articles
WHERE from_search = false
"""

_DENOMINATOR_BY_CATEGORY_SQL = """
SELECT category,
       COUNT(*)::int AS total,
       COUNT(*) FILTER (WHERE jsonb_typeof(entities) = 'object')::int AS extracted
FROM articles
WHERE from_search = false
GROUP BY category
ORDER BY category
"""

# One row per (kind, category, mention). The `tot` CTE applies the min-count
# floor at the mention level so the long tail of single-mention NER noise
# never reaches Python.
_MENTIONS_SQL = """
WITH ex AS (
    SELECT source_url, category, entities
    FROM articles
    WHERE from_search = false
      AND jsonb_typeof(entities) = 'object'
),
m AS (
    SELECT 'person'::text AS kind, category, btrim(v #>> '{}') AS mention
    FROM ex,
         LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(entities -> 'people') = 'array'
                  THEN entities -> 'people' ELSE '[]'::jsonb END
         ) AS t(v)
    WHERE jsonb_typeof(v) = 'string'
    UNION ALL
    SELECT 'organization'::text, category, btrim(v #>> '{}')
    FROM ex,
         LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(entities -> 'organizations') = 'array'
                  THEN entities -> 'organizations' ELSE '[]'::jsonb END
         ) AS t(v)
    WHERE jsonb_typeof(v) = 'string'
),
agg AS (
    SELECT kind,
           category,
           lower(mention) AS mention_norm,
           min(mention)   AS display_form,
           COUNT(*)::int  AS n
    FROM m
    WHERE length(mention) >= $1
    GROUP BY kind, category, lower(mention)
),
tot AS (
    SELECT kind, mention_norm, SUM(n)::int AS article_count
    FROM agg
    GROUP BY kind, mention_norm
)
SELECT a.kind, a.category, a.mention_norm, a.display_form, a.n, t.article_count
FROM agg a
JOIN tot t ON t.kind = a.kind AND t.mention_norm = a.mention_norm
WHERE t.article_count >= $2
ORDER BY t.article_count DESC, a.mention_norm, a.n DESC
"""

_CATALOG_SQL = """
SELECT 'politician' AS etype, bioguide_id AS cid, name FROM politician_profiles
UNION ALL
SELECT 'org', slug, name FROM org_profiles
UNION ALL
SELECT 'outlet', slug, name FROM outlet_profiles
UNION ALL
SELECT 'bill', bill_id, COALESCE(short_title, title) FROM bill_profiles
"""

# Delta for exact matches only. The `@>` containment check rides the
# idx_articles_entity_links_gin index from migration 008.
_LINK_DELTA_SQL = """
WITH pairs(mention_norm, cid) AS (
    SELECT * FROM unnest($1::text[], $2::text[])
),
ex AS (
    SELECT source_url, entities, entity_links
    FROM articles
    WHERE from_search = false
      AND jsonb_typeof(entities) = 'object'
),
m AS (
    SELECT source_url, entity_links, lower(btrim(v #>> '{}')) AS mention_norm
    FROM ex,
         LATERAL jsonb_array_elements(
             (CASE WHEN jsonb_typeof(entities -> 'people') = 'array'
                   THEN entities -> 'people' ELSE '[]'::jsonb END)
             ||
             (CASE WHEN jsonb_typeof(entities -> 'organizations') = 'array'
                   THEN entities -> 'organizations' ELSE '[]'::jsonb END)
         ) AS t(v)
    WHERE jsonb_typeof(v) = 'string'
)
SELECT p.mention_norm,
       p.cid,
       COUNT(DISTINCT m.source_url)::int AS matched_article_count,
       COUNT(DISTINCT m.source_url) FILTER (
           WHERE m.entity_links @> jsonb_build_array(
               jsonb_build_object('canonical_id', p.cid)
           )
       )::int AS linked_article_count
FROM pairs p
JOIN m ON m.mention_norm = p.mention_norm
GROUP BY p.mention_norm, p.cid
"""

_BILL_TEXT_SQL = """
SELECT category, COALESCE(title, '') || ' ' || COALESCE(summary, '') AS text
FROM articles
WHERE from_search = false
"""

_SEARCH_SQL = """
SELECT query_norm,
       COUNT(*)::int                                        AS n,
       COUNT(DISTINCT ip_hash)::int                         AS distinct_ips,
       COUNT(*) FILTER (WHERE result_count_total = 0)::int   AS zero_result,
       ROUND(AVG(query_token_count), 1)::float               AS avg_tokens
FROM search_queries
WHERE created_at > NOW() - ($1::int * INTERVAL '1 day')
  AND COALESCE(user_agent_class, '') <> 'bot'
GROUP BY query_norm
ORDER BY n DESC
LIMIT 500
"""


async def main(
    output_dir: str,
    min_count: int,
    min_extraction_rate: float,
    search_days: int,
    force: bool,
) -> int:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    try:
        # ── 1. The denominator, before anything else ─────────────────────
        d = await pool.fetchrow(_DENOMINATOR_SQL)
        total, extracted = d["total"], d["extracted"]
        rate = (extracted / total) if total else 0.0

        print("Extraction coverage (articles, from_search = false)")
        print(f"  total:                 {total:>8,}")
        print(f"  entities extracted:    {extracted:>8,}  ({rate:.1%})")
        print(f"  never extracted:       {d['never_extracted']:>8,}  (entities still '[]')")
        print(f"  has entity_links:      {d['has_links']:>8,}")
        print()
        print("  Every count below is against the *extracted* subset, not the total.")
        print()

        cat_rows = await pool.fetch(_DENOMINATOR_BY_CATEGORY_SQL)
        cat_denominator = {
            r["category"]: (r["total"], r["extracted"]) for r in cat_rows
        }

        if rate < min_extraction_rate and not force:
            print(
                f"REFUSING TO RANK: extraction coverage {rate:.1%} is below the "
                f"{min_extraction_rate:.0%} floor.\n"
                "A demand signal drawn from a minority of the corpus is not a demand\n"
                "signal. There is no retry sweep for `entities` (unlike backfill_primers.py\n"
                "for primers), so low coverage means failed batches, not a quiet corpus.\n"
                "Backfill extraction first, or re-run with --force to rank anyway."
            )
            return 2

        # ── 2. Mention rollup ────────────────────────────────────────────
        rows = await pool.fetch(_MENTIONS_SQL, _MIN_MENTION_LENGTH, min_count)

        # (kind, mention_norm) -> {display, total, per_category}
        mentions: dict[tuple[str, str], dict] = {}
        for r in rows:
            key = (r["kind"], r["mention_norm"])
            e = mentions.get(key)
            if e is None:
                e = mentions[key] = {
                    "display_form": r["display_form"],
                    "article_count": r["article_count"],
                    "per_category": {},
                }
            e["per_category"][r["category"]] = r["n"]
            # min() keeps the display form stable across category rows
            if r["display_form"] < e["display_form"]:
                e["display_form"] = r["display_form"]

        # ── 3. Catalog anti-join ─────────────────────────────────────────
        catalog_rows = await pool.fetch(_CATALOG_SQL)
        catalog = [(r["etype"], r["cid"], r["name"]) for r in catalog_rows if r["name"]]
        index = CatalogIndex(catalog)
        print(f"Catalog: {len(catalog)} rows across politician / org / outlet / bill.")
        print()

        suggestions: list[dict] = []
        for (kind, mention_norm), e in mentions.items():
            per_cat = e["per_category"]
            top_category = max(per_cat, key=per_cat.get) if per_cat else ""
            stype, scid, status = index.match(e["display_form"])
            suggestions.append({
                "kind": kind,
                "mention": e["display_form"],
                "mention_norm": mention_norm,
                "article_count": e["article_count"],
                "category_spread": len(per_cat),
                "top_category": top_category,
                "catalog_status": status,
                "suggested_type": stype or "",
                "suggested_canonical_id": scid or "",
                "matched_article_count": "",
                "linked_article_count": "",
                "signal": "entity_extractor",
                "proposed_dossier_type": "",
            })

        # ── 4. entity_links delta, exact matches only, reported apart ────
        exact = [s for s in suggestions if s["catalog_status"] == "exact"]
        if exact:
            delta = await pool.fetch(
                _LINK_DELTA_SQL,
                [s["mention_norm"] for s in exact],
                [s["suggested_canonical_id"] for s in exact],
            )
            by_pair = {
                (r["mention_norm"], r["cid"]): (
                    r["matched_article_count"], r["linked_article_count"]
                )
                for r in delta
            }
            for s in exact:
                hit = by_pair.get((s["mention_norm"], s["suggested_canonical_id"]))
                if hit:
                    s["matched_article_count"], s["linked_article_count"] = hit

        # ── 5. Bill regex sweep (the extractor emits no bills) ───────────
        bill_rows = await pool.fetch(_BILL_TEXT_SQL)
        bill_hits: dict[str, dict] = {}
        for r in bill_rows:
            text = r["text"] or ""
            found = {f"H.R. {n}" for n in _BILL_NUMBER_RE.findall(text)}
            found |= set(_BILL_TITLE_RE.findall(text))
            for surface in found:
                e = bill_hits.setdefault(
                    _normalize(surface),
                    {"display": surface, "n": 0, "cats": set()},
                )
                e["n"] += 1
                e["cats"].add(r["category"])

        for norm, e in bill_hits.items():
            if e["n"] < min_count:
                continue
            stype, scid, status = index.match(e["display"])
            suggestions.append({
                "kind": "bill",
                "mention": e["display"],
                "mention_norm": norm,
                "article_count": e["n"],
                "category_spread": len(e["cats"]),
                "top_category": "",
                "catalog_status": status,
                "suggested_type": stype or "",
                "suggested_canonical_id": scid or "",
                "matched_article_count": "",
                "linked_article_count": "",
                "signal": "regex_sweep",
                "proposed_dossier_type": "",
            })

        # ── 6. Search demand ─────────────────────────────────────────────
        search_rows = await pool.fetch(_SEARCH_SQL, search_days)
        unmatched_searches: list[dict] = []
        for r in search_rows:
            stype, scid, status = index.match(r["query_norm"])
            if status == "exact":
                continue
            unmatched_searches.append({
                "query_norm": r["query_norm"],
                "searches": r["n"],
                "distinct_ips": r["distinct_ips"],
                "zero_result": r["zero_result"],
                "avg_tokens": r["avg_tokens"],
                "catalog_status": status,
                "suggested_type": stype or "",
                "suggested_canonical_id": scid or "",
                "proposed_dossier_type": "",
            })

    finally:
        await pool.close()

    # ── Write the three CSVs ─────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    suggestions.sort(key=lambda s: (-s["article_count"], s["mention_norm"]))

    main_path = os.path.join(output_dir, "unlinked_entity_suggestions.csv")
    fields = [
        "kind", "mention", "mention_norm", "article_count", "category_spread",
        "top_category", "catalog_status", "suggested_type",
        "suggested_canonical_id", "matched_article_count",
        "linked_article_count", "signal", "proposed_dossier_type",
    ]
    with open(main_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(suggestions)

    cat_path = os.path.join(output_dir, "unlinked_entities_by_category.csv")
    unlinked = [s for s in suggestions if s["catalog_status"] == "none"]
    per_cat_stats: dict[str, dict] = defaultdict(
        lambda: {"distinct": 0, "volume": 0, "top": []}
    )
    for s in unlinked:
        c = s["top_category"] or "—"
        st = per_cat_stats[c]
        st["distinct"] += 1
        st["volume"] += s["article_count"]
        st["top"].append(s["mention"])
    with open(cat_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "category", "articles_total", "articles_extracted", "extraction_rate",
            "distinct_unlinked_mentions", "unlinked_mention_volume", "top_10_unlinked",
        ])
        for category in sorted(set(cat_denominator) | set(per_cat_stats)):
            tot_c, ext_c = cat_denominator.get(category, (0, 0))
            st = per_cat_stats.get(category, {"distinct": 0, "volume": 0, "top": []})
            w.writerow([
                category, tot_c, ext_c,
                f"{(ext_c / tot_c):.1%}" if tot_c else "—",
                st["distinct"], st["volume"], "; ".join(st["top"][:10]),
            ])

    search_path = os.path.join(output_dir, "unmatched_search_queries.csv")
    with open(search_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "query_norm", "searches", "distinct_ips", "zero_result", "avg_tokens",
            "catalog_status", "suggested_type", "suggested_canonical_id",
            "proposed_dossier_type",
        ])
        w.writeheader()
        w.writerows(unmatched_searches)

    # ── Report ───────────────────────────────────────────────────────────
    by_status: dict[str, int] = defaultdict(int)
    for s in suggestions:
        by_status[s["catalog_status"]] += 1

    print(f"Mentions surviving the >={min_count}-article floor: {len(suggestions)}")
    for status in ("none", "substring", "exact"):
        print(f"  {status:10s}: {by_status[status]}")
    print()
    print(f"Wrote {len(suggestions)} rows to {main_path}")
    print(f"Wrote per-category coverage to {cat_path}")
    print(f"Wrote {len(unmatched_searches)} unmatched queries to {search_path}")
    print()

    print("Top 25 mentions with NO catalog row (the ranking):")
    for s in unlinked[:25]:
        print(f"  {s['article_count']:>5}  {s['kind']:<12} {s['mention'][:48]:<48} "
              f"{s['top_category']}")
    print()

    # The delta block — deliberately printed apart from the ranking above.
    suppressed = [
        s for s in suggestions
        if s["catalog_status"] == "exact"
        and isinstance(s["matched_article_count"], int)
        and s["matched_article_count"] >= min_count
        and s["linked_article_count"] < s["matched_article_count"] * 0.5
    ]
    if suppressed:
        suppressed.sort(key=lambda s: -s["matched_article_count"])
        print("These already have dossiers; the linker is choosing not to chip them.")
        print("Linker tuning, NOT a coverage gap — see the rule-3 suppression list in")
        print("services/entity_linker_llm.py. Not part of the ranking above.")
        for s in suppressed[:15]:
            matched, linked = s["matched_article_count"], s["linked_article_count"]
            print(f"  {linked:>5}/{matched:<5} ({linked / matched:.0%})  "
                  f"{s['mention'][:40]:<40} → {s['suggested_canonical_id']}")
        print()

    print("Caveats to carry into any decision made from this:")
    print("  - `entities` is written AFTER link_entities in the pipeline, so this")
    print("    is a genuinely independent signal — but a failed extraction batch")
    print("    leaves '[]' permanently and there is no retry sweep.")
    print("  - Bill rows are signal=regex_sweep, not extractor-derived: the")
    print("    extractor emits no bills at all.")
    print("  - search_queries.session_id is null in prod, so distinct_ips is the")
    print("    only uniqueness proxy; isLoggingEnabled() has no NODE_ENV guard, so")
    print("    local/preview traffic is mixed in. Retention caps this at 90 days.")
    print()
    print("Next step: fill `proposed_dossier_type` in the suggestions CSV, then")
    print("feed the accepted rows into the relevant seeder.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rank corpus entity mentions that have no dossier."
    )
    parser.add_argument(
        "--output-dir", default="data",
        help="Directory for the three output CSVs (default: data).",
    )
    parser.add_argument(
        "--min-count", type=int, default=3,
        help="Drop mentions appearing in fewer than N articles (default: 3).",
    )
    parser.add_argument(
        "--min-extraction-rate", type=float, default=0.5,
        help="Refuse to rank below this extraction coverage (default: 0.5).",
    )
    parser.add_argument(
        "--search-days", type=int, default=90,
        help="Lookback window for search_queries (default: 90, the retention cap).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Rank even if extraction coverage is below the floor.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(
        args.output_dir, args.min_count, args.min_extraction_rate,
        args.search_days, args.force,
    )))
