"""
Hand-ranked pairs eval for the feed ranking formula (ranking v2 stage 3,
sift/docs/RANKING_SIGNALS.md).

Blind A/B: the reviewer sees two items (title + summary only — no scores, no
source counts, no formula hints) and answers "which is the more important top
story?". Agreement is then computed for BOTH formulas — the pre-v2 ranking
(importance x decay x grim dampener; raw-count stories) and the shipped v2
(saturating corroboration + spectrum bonus + civic-density boost) — against
the same human picks. v2 earns its keep only if it agrees with the human at
least as often as the formula it replaced.

Modes:
    python scripts/eval_ranking_pairs.py --sample            # build pairs from prod
    python scripts/eval_ranking_pairs.py --sample --pairs 25 --hours 48
    python scripts/eval_ranking_pairs.py --score --picks a,b,a,skip,...
    python scripts/eval_ranking_pairs.py --score --picks-file picks.txt

Sampling notes (mirrors scripts/eval_clustering.py's review-pairs rationale):
- Pairs, not lists: a binary better/worse call on ~25 pairs is ~20 minutes.
- Pairs where the two formulas DISAGREE on order are the informative ones and
  are sampled first; same-verdict pairs with narrow margins are kept as
  controls. Random pairs would be trivially decided and inflate agreement.
- Side assignment (which item prints as A) is a hash of the pair identity, so
  neither formula's preferred item is systematically A.
- The machine's answers live only in the JSON, never on the printed sheet.

Pairs file: data/eval/ranking_pairs.json (overwritten by --sample; commit it
so the labeling session is reproducible and re-scorable).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from app.config import settings  # noqa: E402

PAIRS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "eval", "ranking_pairs.json",
)

CATEGORIES = ["top", "politics", "world"]
SUMMARY_SNIPPET = 240

# Formula constants — keep in lockstep with sift/lib/db.ts + NewsAggregator.tsx.
GRIM_DAMPENER = 0.6
STORY_BOOST = 0.8
SPECTRUM_BOOST = 0.1
CIVIC_BOOST = 0.1
CIVIC_WEIGHTS = {"bill": 1.0, "politician": 1.0, "org": 0.5, "outlet": 0.0}


# --- scoring the two formulas -------------------------------------------------

def _decay(published: datetime | None, now: datetime) -> float:
    if published is None:
        age_h = 48.0
    else:
        age_h = max(0.0, (now - published).total_seconds() / 3600)
    return math.exp(-age_h / 24)


def _civic_weight(entity_links: object) -> float:
    if not isinstance(entity_links, list):
        return 0.0
    seen: set[tuple] = set()
    total = 0.0
    for el in entity_links:
        if not isinstance(el, dict):
            continue
        key = (el.get("type"), el.get("canonical_id"))
        if key in seen:
            continue
        seen.add(key)
        total += CIVIC_WEIGHTS.get(el.get("type"), 0.0)
    return total


def _civic_boost(weight: float) -> float:
    return 1 + CIVIC_BOOST * min(max(weight, 0.0), 3.0)


def article_scores(row: dict, now: datetime) -> tuple[float, float]:
    """(old, new) visible-rank scores for a standalone article."""
    imp = row["importance_score"] or 3
    damp = GRIM_DAMPENER if row["tone"] == "grim" and imp <= 3 else 1.0
    base = imp * _decay(row["published_date"], now) * damp
    return base, base * _civic_boost(_civic_weight(row["entity_links"]))


def story_scores(row: dict, now: datetime) -> tuple[float, float]:
    """(old, new) visible-rank scores for a story.

    old = the pre-v2 client formula (3 + 0.5*min(n-1,4));
    new = stage 1 + 2 (saturating curve, spectrum bonus, member civic max).
    """
    n = row["sources"]
    damp = GRIM_DAMPENER if row["grim"] and n <= 2 else 1.0
    d = _decay(row["published_date"], now)
    old = (3 + 0.5 * min(n - 1, 4)) * d * damp
    spectrum = 1 + SPECTRUM_BOOST * max(0, row["buckets"] - 1)
    new = (3 + STORY_BOOST * math.log(1 + n)) * d * spectrum * damp * _civic_boost(row["max_member_civic"])
    return old, new


# --- sampling -----------------------------------------------------------------

_ARTICLES = """
SELECT id, title, summary, published_date, importance_score, tone, entity_links
FROM articles
WHERE category = $1 AND from_search = false AND story_id IS NULL
  AND summary IS NOT NULL AND summary != ''
  AND LOWER(summary) NOT LIKE 'unable to provide%'
  AND (published_date > NOW() - make_interval(hours => $2)
       OR (published_date IS NULL AND created_at > NOW() - make_interval(hours => $2)))
"""

_STORIES = """
SELECT s.id, s.headline AS title, s.summary, s.published_date, s.framings,
       COUNT(a.id)::int AS sources,
       AVG(CASE WHEN a.tone = 'grim' THEN 1.0 ELSE 0 END) >= 0.5 AS grim,
       MAX(COALESCE((
         SELECT SUM(CASE t WHEN 'bill' THEN 1.0 WHEN 'politician' THEN 1.0 WHEN 'org' THEN 0.5 ELSE 0 END)
         FROM (SELECT DISTINCT el->>'type' AS t, el->>'canonical_id' AS cid
               FROM jsonb_array_elements(CASE WHEN jsonb_typeof(a.entity_links) = 'array' THEN a.entity_links ELSE '[]'::jsonb END) el) links
       ), 0)) AS max_member_civic
FROM stories s
LEFT JOIN articles a ON a.story_id = s.id AND a.from_search = false
  AND a.summary IS NOT NULL AND a.summary != ''
WHERE s.category = $1 AND s.synthesis_status = 'complete'
  AND s.published_date > NOW() - make_interval(hours => $2)
GROUP BY s.id HAVING COUNT(a.id) >= 2
"""

_ALIAS_RATINGS = """
SELECT sna.raw_source_name, op.allsides_rating
FROM source_name_aliases sna JOIN outlet_profiles op ON op.slug = sna.outlet_slug
"""


def _bucket(rating: str | None) -> str | None:
    if rating in ("left", "lean-left"):
        return "left"
    if rating == "center":
        return "center"
    if rating in ("right", "lean-right"):
        return "right"
    return None


def _story_buckets(framings: object, ratings: dict[str, str | None]) -> int:
    if isinstance(framings, str):
        try:
            framings = json.loads(framings)
        except json.JSONDecodeError:
            return 0
    if not isinstance(framings, list):
        return 0
    occupied = set()
    for f in framings:
        if isinstance(f, dict):
            b = _bucket(ratings.get(str(f.get("source_name"))))
            if b:
                occupied.add(b)
    return len(occupied)


def _pair_id(a: str, b: str) -> str:
    return hashlib.sha256(f"{min(a, b)}|{max(a, b)}".encode()).hexdigest()[:10]


def _snip(text: str | None) -> str:
    return (text or "")[:SUMMARY_SNIPPET]


async def sample(pairs_wanted: int, hours: int) -> None:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    now = await conn.fetchval("SELECT NOW()")
    ratings = {
        r["raw_source_name"]: r["allsides_rating"]
        for r in await conn.fetch(_ALIAS_RATINGS)
    }

    items: list[dict] = []
    for cat in CATEGORIES:
        for row in await conn.fetch(_ARTICLES, cat, hours):
            r = dict(row)
            old, new = article_scores(r, now)
            items.append({
                "id": r["id"], "kind": "article", "category": cat,
                "title": r["title"], "summary": _snip(r["summary"]),
                "old": old, "new": new,
            })
        for row in await conn.fetch(_STORIES, cat, hours):
            r = dict(row)
            r["buckets"] = _story_buckets(r["framings"], ratings)
            r["max_member_civic"] = float(r["max_member_civic"] or 0)
            old, new = story_scores(r, now)
            items.append({
                "id": r["id"], "kind": "story", "category": cat,
                "title": r["title"], "summary": _snip(r["summary"]),
                "old": old, "new": new,
            })
    await conn.close()

    # Candidate pairs: same kind + category. Disagreements first (the two
    # formulas order the pair differently), then narrow-margin agreements as
    # controls. Both sorted deterministically.
    disagreements: list[dict] = []
    controls: list[dict] = []
    by_group: dict[tuple, list[dict]] = {}
    for it in items:
        by_group.setdefault((it["kind"], it["category"]), []).append(it)
    for group in by_group.values():
        group.sort(key=lambda x: x["id"])
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                old_pick = "a" if a["old"] >= b["old"] else "b"
                new_pick = "a" if a["new"] >= b["new"] else "b"
                margin = abs(a["new"] - b["new"])
                pair = {
                    "pair_id": _pair_id(a["id"], b["id"]),
                    "kind": a["kind"], "category": a["category"],
                    "a": {"id": a["id"], "title": a["title"], "summary": a["summary"]},
                    "b": {"id": b["id"], "title": b["title"], "summary": b["summary"]},
                    "old_pick": old_pick, "new_pick": new_pick,
                    "disagreement": old_pick != new_pick,
                    "margin": round(margin, 4),
                }
                (disagreements if pair["disagreement"] else controls).append(pair)

    # Deterministic order inside each bucket; controls prefer narrow margins
    # (a pair both formulas call 4.1-vs-0.2 teaches nothing).
    disagreements.sort(key=lambda p: p["pair_id"])
    controls.sort(key=lambda p: (p["margin"], p["pair_id"]))

    take_dis = min(len(disagreements), max(1, pairs_wanted * 2 // 3))
    chosen = disagreements[:take_dis] + controls[: pairs_wanted - take_dis]

    # Shuffle the final sheet by hash and re-letter sides by hash so neither
    # position nor bucket order leaks anything.
    chosen.sort(key=lambda p: hashlib.sha256(("sheet" + p["pair_id"]).encode()).hexdigest())
    for p in chosen:
        if int(hashlib.sha256(("side" + p["pair_id"]).encode()).hexdigest(), 16) % 2:
            p["a"], p["b"] = p["b"], p["a"]
            p["old_pick"] = "b" if p["old_pick"] == "a" else "a"
            p["new_pick"] = "b" if p["new_pick"] == "a" else "a"

    os.makedirs(os.path.dirname(PAIRS_PATH), exist_ok=True)
    with open(PAIRS_PATH, "w") as f:
        json.dump({
            "generated_at": now.isoformat(),
            "hours": hours,
            "pairs": chosen,
        }, f, indent=2, default=str)

    print(f"Wrote {len(chosen)} pairs ({sum(1 for p in chosen if p['disagreement'])} "
          f"formula disagreements, {sum(1 for p in chosen if not p['disagreement'])} controls) "
          f"to {os.path.relpath(PAIRS_PATH)}\n")
    print("Blind sheet — answer 'a' or 'b' per pair (or 'skip'):\n")
    for i, p in enumerate(chosen, 1):
        print(f"{i:2}. [{p['category']}/{p['kind']}]")
        print(f"    A: {p['a']['title']}")
        print(f"       {p['a']['summary'][:160]}")
        print(f"    B: {p['b']['title']}")
        print(f"       {p['b']['summary'][:160]}\n")


# --- scoring ------------------------------------------------------------------

def score(picks: list[str]) -> None:
    with open(PAIRS_PATH) as f:
        data = json.load(f)
    pairs = data["pairs"]
    if len(picks) != len(pairs):
        raise SystemExit(f"{len(pairs)} pairs on file but {len(picks)} picks given")

    scored = [(p, pick) for p, pick in zip(pairs, picks) if pick in ("a", "b")]
    skipped = len(pairs) - len(scored)
    if not scored:
        raise SystemExit("no scorable picks")

    old_agree = sum(1 for p, pick in scored if p["old_pick"] == pick)
    new_agree = sum(1 for p, pick in scored if p["new_pick"] == pick)
    dis = [(p, pick) for p, pick in scored if p["disagreement"]]
    dis_new = sum(1 for p, pick in dis if p["new_pick"] == pick)

    n = len(scored)
    print(f"Scored {n} pairs ({skipped} skipped).")
    print(f"  pre-v2 formula agrees with you on {old_agree}/{n} ({old_agree / n:.0%})")
    print(f"  v2 formula     agrees with you on {new_agree}/{n} ({new_agree / n:.0%})")
    if dis:
        print(f"  on the {len(dis)} pairs where the formulas disagree, "
              f"you sided with v2 on {dis_new} ({dis_new / len(dis):.0%})")
    print()
    if n < 20:
        print("Caution: under 20 scored pairs — treat this as a smell test, not a verdict.")
    elif abs(new_agree - old_agree) <= 2:
        print("The formulas are within 2 picks of each other at this sample size — "
              "that is a tie, not a winner. Do not quote a percentage.")
    elif new_agree > old_agree:
        print("v2 agrees with your judgment more often than the formula it replaced.")
    else:
        print("v2 agrees with your judgment LESS often than the formula it replaced — "
              "revisit the constants before trusting it further.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sample", action="store_true", help="Build pairs from prod and print the blind sheet.")
    mode.add_argument("--score", action="store_true", help="Score picks against both formulas.")
    parser.add_argument("--pairs", type=int, default=25)
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--picks", help="Comma-separated picks: a,b,skip,...")
    parser.add_argument("--picks-file", help="File with one pick per line.")
    args = parser.parse_args()

    if args.sample:
        asyncio.run(sample(args.pairs, args.hours))
        return
    if args.picks:
        picks = [p.strip().lower() for p in args.picks.split(",")]
    elif args.picks_file:
        with open(args.picks_file) as f:
            picks = [line.strip().lower() for line in f if line.strip()]
    else:
        raise SystemExit("--score needs --picks or --picks-file")
    score(picks)


if __name__ == "__main__":
    main()
