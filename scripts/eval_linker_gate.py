"""Measure the recall cost of putting the regex matcher in FRONT of the LLM linker.

WHY THIS EXISTS
---------------
`services/entity_linker_llm.link_articles_llm` makes one realtime Claude call
per article. At the measured ingest rate (~2,000 new articles/day, not the
~100/day its docstring assumes) that is the single largest line item in the
repo — $4.15/day of a $8.99/day total, 46%, per `ai_usage_daily` over
2026-07-31..08-04.

Only 13.3% of articles end up with any `entity_links` at all, so ~87% of those
calls resolve to nothing. The proposed fix is a free pre-gate: run the existing
regex matcher first and send an article to the LLM only when the regex finds at
least one candidate surface form.

The argument for why that is safe: `entity_linker_llm`'s own docstring says the
LLM exists to *disambiguate* full-name collisions ("Susan Collins" the Boston
Fed president vs. the Senator), not to *discover* entities whose names never
appear. If no catalog surface form occurs in the text, there is nothing to
disambiguate.

The argument for why it might not be: PR #40 deliberately removed mechanically
derived last-name aliases, so the regex will not match "Warren said" — while
the LLM plausibly would. That is a recall hole of unknown size.

THIS SCRIPT MEASURES THE HOLE. It costs nothing: no API calls, read-only SQL,
and it scores the gate against `entity_links` ALREADY stored — which was the
LLM's output alone until 2026-08-05. It no longer is; see the second caveat.

    recall     = of articles where the LLM stored >=1 link,
                 the share where the regex finds >=1 candidate
                 (i.e. the share the gate would still forward to the LLM)
    passthru   = of ALL articles, the share the gate forwards
                 (i.e. what you still pay for)

SHIP BAR: recall >= 95%.

THE FALLBACK THIS USED TO NAME IS GONE. It read "below that, do not gate —
batch the linker instead (~10 articles/call, modeled at -60% in STATUS.md),
which has no recall risk." Batching was built and A/B'd on 2026-08-11 and its
recall risk is large: **79.5% at 10/call, 83.6% at 5**, against a single-
article path that agrees with itself 97.3% across two runs. Not a batch-size
effect — a batch of *two* loses ~20 points — and not a position effect. It was
reverted; the experiment is in docs/SOURCE_SCALING.md.

So the gate is not being weighed against a free alternative. The live one is
**roster narrowing** — send the regex's own candidates plus their collision
siblings instead of all 856 rows — measured at 94.2% recall and ~22 roster
tokens per call, un-shipped pending a precision check. It composes *with* this
gate rather than replacing it, since it consumes the gate's match output.

READ THE RECALL NUMBER WITH THIS CAVEAT
---------------------------------------
`entity_aliases` (migration 014) was seeded 2026-08-05. Stored links older than
that were produced by an LLM that did NOT have the curated aliases, while the
regex here DOES. That skews the two sides in opposite directions and it is not
a wash, so the report splits recall by whether the article predates the seed.
Prefer the post-seed number once enough articles have accumulated; treat the
pooled number as a lower bound in the meantime.

AND THIS LARGER ONE: SINCE 2026-08-05 THE RECALL NUMBER IS INFLATED
------------------------------------------------------------------
The metric assumes stored links came from the LLM, so that "did the regex also
find one" is an independent question. `backfill_entity_links.py --include-empty`
(#141) ran the **regex** over the whole corpus and wrote its output to
`entity_links` for 54,240 articles. Those stored links are by construction
re-findable by the regex that produced them, so the script now partly scores
the backfill against itself.

The signature is visible in the split: post-alias-seed recall read 99.91% on
n=1,140 the first time this ran afterwards, against 97.63% pooled on the
pre-backfill corpus (n=6 post-seed). Near-perfect is contamination, not
improvement.

**Consequence: a recall figure taken before the backfill is not reproducible
by re-running this script, and a higher number afterwards is not evidence of a
better gate.** Those earlier reads were clean; they are history, not something
to re-derive here. Do not compare a pre-backfill number against a post-backfill
one — that includes the before/after pairs already quoted in STATUS.md.

Restoring a clean measurement needs a real change, not a doc fix — the scored
set has to exclude links the regex wrote. There is no column recording which
path produced a link, so that means either provenance on `entity_links` or an
LLM-only re-link of a held-out sample. Until then, read this script for
pass-through and for *misses* (which are still real and still diagnostic), and
do not quote its recall as a before/after.

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/eval_linker_gate.py
    ./.venv/bin/python3 scripts/eval_linker_gate.py --days 14 --show-misses 40
    ./.venv/bin/python3 scripts/eval_linker_gate.py --json data/_cache/gate.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

# scripts/ sits next to services/ — make the latter importable from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from services.entity_linker import build_catalog, build_search_dict, link_text  # noqa: E402

# When entity_aliases was seeded. Articles linked before this saw a smaller
# catalog than the gate does, so their recall is not directly comparable.
ALIAS_SEED_DEFAULT = "2026-08-05T06:12:11+00:00"

SHIP_BAR = 0.95


def _as_links(raw: object) -> list[dict]:
    """`entity_links` is JSONB; asyncpg hands back str or list depending on codec."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


async def _load_catalog(conn: asyncpg.Connection) -> list[dict]:
    """Same four dossier queries the pipeline node uses, plus curated aliases."""
    outlets = [dict(r) for r in await conn.fetch("SELECT slug, name FROM outlet_profiles")]
    politicians = [dict(r) for r in await conn.fetch(
        "SELECT bioguide_id, name FROM politician_profiles")]
    orgs = [dict(r) for r in await conn.fetch("SELECT slug, name FROM org_profiles")]
    bills = [dict(r) for r in await conn.fetch(
        "SELECT bill_id, title, short_title FROM bill_profiles")]

    # Tolerated missing so a pre-014 database still scores on canonical names.
    try:
        aliases = [dict(r) for r in await conn.fetch(
            "SELECT alias, entity_type, canonical_id, match_case FROM entity_aliases")]
    except asyncpg.UndefinedTableError:
        aliases = []

    print(f"catalog: {len(outlets)} outlets, {len(politicians)} politicians, "
          f"{len(orgs)} orgs, {len(bills)} bills, {len(aliases)} curated aliases")
    return build_catalog(outlets, politicians, orgs, bills, aliases)


def _pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


async def main(days: int, show_misses: int, alias_seed: str, out_json: str | None) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    seed_at = datetime.fromisoformat(alias_seed)
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        catalog = await _load_catalog(conn)
        search_dict = build_search_dict(catalog)
        print(f"search_dict: {len(search_dict)} unambiguous surface forms\n")

        rows = await conn.fetch(
            """
            SELECT id, title, summary, entity_links, created_at, category
            FROM articles
            WHERE created_at > NOW() - ($1::text || ' days')::interval
              AND from_search = false
            """,
            str(days),
        )
    finally:
        await conn.close()

    if not rows:
        print(f"No articles in the last {days} days — nothing to score.")
        return

    # Buckets: `linked` = LLM stored >=1 link (our ground-truth positives).
    total = passthru = linked = linked_passthru = 0
    pre_linked = pre_hit = post_linked = post_hit = 0
    link_total = link_found = 0
    misses: list[dict] = []

    for r in rows:
        total += 1
        stored = _as_links(r["entity_links"])
        text = f"{r['title'] or ''}\n{r['summary'] or ''}"
        candidates = link_text(text, search_dict)
        forwarded = bool(candidates)
        if forwarded:
            passthru += 1

        if not stored:
            continue

        linked += 1
        is_post = r["created_at"] >= seed_at
        if is_post:
            post_linked += 1
        else:
            pre_linked += 1

        if forwarded:
            linked_passthru += 1
            if is_post:
                post_hit += 1
            else:
                pre_hit += 1
        elif len(misses) < show_misses:
            misses.append({
                "id": r["id"],
                "category": r["category"],
                "created_at": r["created_at"].isoformat(),
                "title": (r["title"] or "")[:100],
                "stored": [f"{s.get('type')}:{s.get('canonical_id')}" for s in stored],
            })

        # Link-level: would the regex surface the same canonical_id? Stricter
        # than the gate needs (the gate only has to forward the article), but
        # it shows whether a pass is real agreement or a lucky unrelated hit.
        found = {(c["type"], c["canonical_id"]) for c in candidates}
        for s in stored:
            link_total += 1
            if (s.get("type"), s.get("canonical_id")) in found:
                link_found += 1

    recall = _pct(linked_passthru, linked)
    report = {
        "days": days,
        "articles": total,
        "articles_with_stored_links": linked,
        "stored_link_rate_pct": round(_pct(linked, total), 2),
        "gate_passthru_pct": round(_pct(passthru, total), 2),
        "article_recall_pct": round(recall, 2),
        "article_recall_pre_alias_seed_pct": round(_pct(pre_hit, pre_linked), 2),
        "article_recall_post_alias_seed_pct": round(_pct(post_hit, post_linked), 2),
        "post_alias_seed_sample": post_linked,
        "link_level_agreement_pct": round(_pct(link_found, link_total), 2),
        "ship_bar_pct": SHIP_BAR * 100,
        "passes": recall >= SHIP_BAR * 100,
    }

    print(f"articles scored           {total:,} (last {days}d)")
    print(f"  with stored links       {linked:,}  ({report['stored_link_rate_pct']}%)")
    print()
    print(f"GATE PASS-THROUGH         {report['gate_passthru_pct']}%  "
          f"→ LLM calls {total:,} → {passthru:,}")
    print(f"ARTICLE RECALL            {report['article_recall_pct']}%  "
          f"(bar {SHIP_BAR * 100:.0f}%)  {'PASS' if report['passes'] else 'FAIL'}")
    print(f"  pre-alias-seed          {report['article_recall_pre_alias_seed_pct']}%  "
          f"(n={pre_linked:,})")
    print(f"  post-alias-seed         {report['article_recall_post_alias_seed_pct']}%  "
          f"(n={post_linked:,})  ← prefer this once n is large")
    print(f"link-level agreement      {report['link_level_agreement_pct']}%  "
          f"({link_found:,}/{link_total:,} stored links also found by regex)")

    if misses:
        print(f"\nMISSES — LLM linked, regex found nothing (first {len(misses)}):")
        for m in misses:
            print(f"  [{m['category']}] {m['title']}")
            print(f"      → {', '.join(m['stored'])}")

    if out_json:
        os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
        with open(out_json, "w") as fh:
            json.dump({**report, "misses": misses}, fh, indent=2)
        print(f"\nwrote {out_json}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    p.add_argument("--show-misses", type=int, default=20,
                   help="how many recall misses to print for hand-review (default 20)")
    p.add_argument("--alias-seed", default=ALIAS_SEED_DEFAULT,
                   help="ISO timestamp entity_aliases was seeded; splits the recall report")
    p.add_argument("--json", dest="out_json", help="also write the report here")
    args = p.parse_args()
    asyncio.run(main(args.days, args.show_misses, args.alias_seed, args.out_json))
