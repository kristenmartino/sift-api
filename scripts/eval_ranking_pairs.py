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
from datetime import datetime

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
#
# THEY WERE NOT, WHICH IS WHY THIS BLOCK IS NOW ANNOTATED. Between session 2 and
# 2026-08-11 the shipped formula gained stages 4-7 plus a constants retune, and
# this file still scored STORY_BOOST = 0.8, a hardcoded base of 3, no
# opinion/roundup/low-importance/genre dampeners, no importance multiplier, and
# story `sources` counted as ARTICLE ROWS — the exact quantity sift#230 replaced
# with distinct outlets. A session run against that would have spent a human's
# afternoon scoring a formula that was never deployed.
#
# "Keep in lockstep" in a comment is not a mechanism. Every constant now names
# the stage and PR that set it, so drift is at least visible in a diff.
GRIM_DAMPENER = 0.6                 # D48
SPECTRUM_BOOST = 0.1                # stage 1
CIVIC_BOOST = 0.1                   # stage 2
CIVIC_WEIGHTS = {"bill": 1.0, "politician": 1.0, "org": 0.5, "outlet": 0.0}
OPINION_DAMPENER = 0.6              # stage 4, sift#227
ROUNDUP_DAMPENER = 0.4              # stage 5, sift#228
LOW_IMPORTANCE_DAMPENER = 0.35      # stage 6, sift#229 — 'top' only, imp <= 2
NON_NEWS_DAMPENER = 0.5             # stage 6, sift#229 — genre feature|soft
STORY_BASE = 1                      # sift#231 (was a hardcoded 3)
STORY_BOOST = 2.0                   # sift#231 (was 0.8)
STORY_IMPORTANCE_CENTER = 2.5       # stage 7, sift#232
STORY_FLOOR_MIN_IMPORTANCE = 4      # stage 7 floor, sift#237

# Historic constants, kept so the baseline formulas stay reproducible.
LEGACY_STORY_BASE = 3
LEGACY_STORY_BOOST = 0.8

# The three formulas a session scores. `pre_v2` is where this eval started;
# `v2_stage5` is what was deployed before 2026-08-11; `current` is live. Two
# baselines rather than one because the interesting question has moved: not
# "was v2 worth it" (sessions 1-2 answered that, a tie) but "did the four
# changes on 2026-08-11 help".
FORMULAS = ("pre_v2", "v2_stage5", "current")

# Bump when any formula's definition changes, so a stale pairs file cannot be
# scored under semantics it was not generated with.
PAIRS_VERSION = 3


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


def article_scores(row: dict, now: datetime, category: str) -> dict[str, float]:
    """Visible-rank score per formula for a standalone article."""
    imp = row["importance_score"] or 3
    d = _decay(row["published_date"], now)
    grim = GRIM_DAMPENER if row["tone"] == "grim" and imp <= 3 else 1.0
    civic = _civic_boost(_civic_weight(row["entity_links"]))
    opinion = OPINION_DAMPENER if row["is_opinion"] else 1.0
    roundup = ROUNDUP_DAMPENER if row["is_roundup"] else 1.0
    # Stage 6 is scoped to the front page: an importance-2 sports result
    # belongs in Sports, and the topical tabs stay complete coverage.
    low = LOW_IMPORTANCE_DAMPENER if category == "top" and imp <= 2 else 1.0
    genre = NON_NEWS_DAMPENER if row["genre"] in ("feature", "soft") else 1.0

    pre_v2 = imp * d * grim
    v2_stage5 = pre_v2 * civic * opinion * roundup
    current = v2_stage5 * low * genre
    return {"pre_v2": pre_v2, "v2_stage5": v2_stage5, "current": current}


def story_scores(row: dict, now: datetime) -> dict[str, float]:
    """Visible-rank score per formula for a story.

    The three differ in what "corroboration" even means:

    * `pre_v2`    — 3 + 0.5*min(n-1,4) over ARTICLE ROWS, the original client
                    formula.
    * `v2_stage5` — the saturating curve at (3, 0.8), still over article rows,
                    plus spectrum, civic and the opinion dampener.
    * `current`   — (1, 2.0) over DISTINCT OUTLETS (sift#230/#231), multiplied
                    by mean member importance / 2.5 (stage 7) and floored at
                    the best member's importance when that member is >= 4
                    (sift#237).
    """
    rows_n = row["article_count"]
    outlets = row["outlets"]
    d = _decay(row["published_date"], now)
    spectrum = 1 + SPECTRUM_BOOST * max(0, row["buckets"] - 1)
    civic = _civic_boost(row["max_member_civic"])
    opinion = OPINION_DAMPENER if row["is_opinion"] else 1.0

    # The grim dampener's variable moved with the rest: it read article rows
    # while its own comment described outlets (sift#230).
    grim_legacy = GRIM_DAMPENER if row["grim"] and rows_n <= 2 else 1.0
    grim_current = GRIM_DAMPENER if row["grim"] and outlets <= 2 else 1.0

    pre_v2 = (3 + 0.5 * min(rows_n - 1, 4)) * d * grim_legacy
    v2_stage5 = (
        (LEGACY_STORY_BASE + LEGACY_STORY_BOOST * math.log(1 + rows_n))
        * d * spectrum * grim_legacy * civic * opinion
    )

    coverage = STORY_BASE + STORY_BOOST * math.log(1 + outlets)
    significance = coverage * (row["avg_importance"] / STORY_IMPORTANCE_CENTER)
    if row["max_importance"] >= STORY_FLOOR_MIN_IMPORTANCE:
        significance = max(significance, float(row["max_importance"]))
    current = significance * d * spectrum * grim_current * civic * opinion

    return {"pre_v2": pre_v2, "v2_stage5": v2_stage5, "current": current}


# --- sampling -----------------------------------------------------------------

_ARTICLES = """
SELECT id, title, summary, published_date, importance_score, tone, entity_links,
       is_opinion, is_roundup, genre
FROM articles
WHERE category = $1 AND from_search = false AND story_id IS NULL
  AND summary IS NOT NULL AND summary != ''
  AND LOWER(summary) NOT LIKE 'unable to provide%'
  AND (published_date > NOW() - make_interval(hours => $2)
       OR (published_date IS NULL AND created_at > NOW() - make_interval(hours => $2)))
"""

_STORIES = """
SELECT s.id, s.headline AS title, s.summary, s.published_date, s.framings,
       COUNT(a.id)::int AS article_count,
       COUNT(DISTINCT a.source_name)::int AS outlets,
       COALESCE(AVG(a.importance_score), 3)::float AS avg_importance,
       MAX(COALESCE(a.importance_score, 3))::int AS max_importance,
       AVG(CASE WHEN a.is_opinion THEN 1.0 ELSE 0 END) >= 0.5 AS is_opinion,
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


def _stratified(pool: list[dict], want: int) -> list[dict]:
    """Take `want` pairs spread evenly across (kind, category) groups.

    WHY THIS IS NOT A `[:want]` SLICE. It was, and the first session-3 sheet
    came out **24 of 25 politics articles, one story, nothing from 'top'** —
    because pair count grows with the square of a group's item count, so the
    busiest category swamps a flat take. That sheet could not have answered the
    question it was generated for: every change on 2026-08-11 (stages 1 and 7,
    the constants, the floor) is about STORY ranking, and it contained one
    story.

    Round-robins the groups instead, so a thin category still gets a slot and
    stories are never crowded out by articles. Groups keep their incoming
    order — disagreements by pair_id, controls by margin — so the result is
    still deterministic.
    """
    groups: dict[tuple, list[dict]] = {}
    for p in pool:
        groups.setdefault((p["kind"], p["category"]), []).append(p)
    order = sorted(groups)
    out: list[dict] = []
    i = 0
    while len(out) < want and any(groups[k] for k in order):
        bucket = groups[order[i % len(order)]]
        if bucket:
            out.append(bucket.pop(0))
        i += 1
    return out


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
            items.append({
                "id": r["id"], "kind": "article", "category": cat,
                "title": r["title"], "summary": _snip(r["summary"]),
                "scores": article_scores(r, now, cat),
            })
        for row in await conn.fetch(_STORIES, cat, hours):
            r = dict(row)
            r["buckets"] = _story_buckets(r["framings"], ratings)
            r["max_member_civic"] = float(r["max_member_civic"] or 0)
            items.append({
                "id": r["id"], "kind": "story", "category": cat,
                "title": r["title"], "summary": _snip(r["summary"]),
                "scores": story_scores(r, now),
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
                picks = {
                    f: ("a" if a["scores"][f] >= b["scores"][f] else "b")
                    for f in FORMULAS
                }
                margin = abs(a["scores"]["current"] - b["scores"]["current"])
                pair = {
                    "pair_id": _pair_id(a["id"], b["id"]),
                    "kind": a["kind"], "category": a["category"],
                    "a": {"id": a["id"], "title": a["title"], "summary": a["summary"]},
                    "b": {"id": b["id"], "title": b["title"], "summary": b["summary"]},
                    "picks": picks,
                    # The pair is informative when CURRENT disagrees with the
                    # formula it replaced. Disagreement with pre_v2 is settled
                    # history and would just re-litigate sessions 1-2.
                    "disagreement": picks["current"] != picks["v2_stage5"],
                    "margin": round(margin, 4),
                }
                (disagreements if pair["disagreement"] else controls).append(pair)

    # Deterministic order inside each bucket; controls prefer narrow margins
    # (a pair both formulas call 4.1-vs-0.2 teaches nothing).
    disagreements.sort(key=lambda p: p["pair_id"])
    controls.sort(key=lambda p: (p["margin"], p["pair_id"]))

    take_dis = min(len(disagreements), max(1, pairs_wanted * 2 // 3))
    chosen = _stratified(disagreements, take_dis) + _stratified(
        controls, pairs_wanted - take_dis
    )

    # Shuffle the final sheet by hash and re-letter sides by hash so neither
    # position nor bucket order leaks anything.
    chosen.sort(key=lambda p: hashlib.sha256(("sheet" + p["pair_id"]).encode()).hexdigest())
    for p in chosen:
        if int(hashlib.sha256(("side" + p["pair_id"]).encode()).hexdigest(), 16) % 2:
            p["a"], p["b"] = p["b"], p["a"]
            p["picks"] = {
                f: ("b" if v == "a" else "a") for f, v in p["picks"].items()
            }

    os.makedirs(os.path.dirname(PAIRS_PATH), exist_ok=True)
    with open(PAIRS_PATH, "w") as f:
        json.dump({
            "version": PAIRS_VERSION,
            "formulas": list(FORMULAS),
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

    scored = [(p, pick) for p, pick in zip(pairs, picks, strict=False) if pick in ("a", "b")]
    skipped = len(pairs) - len(scored)
    if not scored:
        raise SystemExit("no scorable picks")

    formulas = data.get("formulas")
    if data.get("version") != PAIRS_VERSION or not formulas:
        raise SystemExit(
            f"pairs file is version {data.get('version')!r}, this script expects "
            f"{PAIRS_VERSION}. The formulas changed since it was generated, so "
            "scoring it would report agreement with something that is not "
            "deployed. Re-run --sample."
        )

    n = len(scored)
    agree = {
        f: sum(1 for p, pick in scored if p["picks"][f] == pick) for f in formulas
    }
    dis = [(p, pick) for p, pick in scored if p["disagreement"]]

    print(f"Scored {n} pairs ({skipped} skipped).\n")
    labels = {
        "pre_v2": "pre-v2 (importance x decay)",
        "v2_stage5": "v2 as of stages 1-5",
        "current": "current (stages 1-7 + floor)",
    }
    for f in formulas:
        print(f"  {labels.get(f, f):32} {agree[f]:2}/{n} ({agree[f] / n:.0%})")
    if dis:
        dis_cur = sum(1 for p, pick in dis if p["picks"]["current"] == pick)
        print(f"\n  on the {len(dis)} pairs where CURRENT disagrees with stages 1-5, "
              f"you sided with current on {dis_cur} ({dis_cur / len(dis):.0%})")

    print()
    if n < 20:
        print("Caution: under 20 scored pairs — a smell test, not a verdict.")
        return
    # The comparison that matters is current vs the formula it replaced. pre_v2
    # is carried for continuity with sessions 1-2, not as the bar.
    delta = agree["current"] - agree["v2_stage5"]
    if abs(delta) <= 2:
        print("current and stages 1-5 are within 2 picks at this sample size — "
              "that is a tie, not a winner. Do not quote a percentage.")
    elif delta > 0:
        print("current agrees with your judgment more often than the formula it "
              "replaced. The 2026-08-11 changes earned their keep.")
    else:
        print("current agrees with your judgment LESS often than the formula it "
              "replaced — revisit the 2026-08-11 constants before trusting them "
              "further.")


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
