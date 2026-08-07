"""Free kNN candidate generation for incremental story threading.

WHAT THIS REPLACES
------------------
`workflows/story_workflow.py` sends the newest 50 articles in a category to
Claude and asks it to partition them. Three measured problems, 2026-08-05:

  * `LIMIT 50 ORDER BY published_date DESC` makes the nominal 48h window
    **~3.3h for politics and ~3.7h for sports**, so same-event articles hours
    apart never appear in one prompt. Sports groups at 0.9%.
  * The run repeats every 30 minutes over a near-identical slice, so cost
    scales with *cadence x window* rather than with new articles. Threading was
    43% of Anthropic spend.
  * `story_id` is a sha256 of the member set, so any membership change mints a
    new row. 58,259 of 58,557 `stories` rows (99.5%) have no members.

This module does the candidate step in Postgres instead, for $0, over the
**full** 48h pool rather than a 50-row slice.

THE PIVOTAL PROPERTY
--------------------
Each new article searches *backward*. Old articles are never re-queued — but
they stay searchable, so a singleton from 20 hours ago is pulled in when a
matching new article arrives and finds it. Work per run is O(new articles),
not O(window x categories), which is what preserves 30-minute freshness
without paying for it.

WHY THIS DOES NOT NEED THE IVFFLAT INDEX
----------------------------------------
Measured 2026-08-07 with EXPLAIN ANALYZE on all four representative
categories: the planner never uses `idx_articles_embedding` for this query
shape. `category = $1 AND published_date > NOW() - 48h` is selective enough
(64-1,103 rows) that it filters through `idx_articles_category_date` and then
does an exact top-N heapsort on cosine distance — 8.5ms for the largest pool.

So recall is **100% by construction**, not an approximation to be tuned. The
prerequisite in docs/INCREMENTAL_THREADING.md to verify ivfflat recall@10 does
not apply here; that index matters for whole-corpus topic search, which is a
different query with no filter. Nothing here depends on `lists` or
`ivfflat.probes`.

THRESHOLD
---------
0.60, calibrated against 283 pairs the current LLM clusterer actually grouped:
~90% of them score at or above it. The cost curve is flat where the recall
curve is steep — 0.60 to 0.80 saves ~$0.72/day and costs 57 points of recall —
so this tunes for recall. An earlier draft proposed 0.75-0.85; measuring the
curve overturned it.

Known bias: those pairs were only ever found inside a ~3.3h window, so the
corpus under-represents exactly the long-range matches this design exists to
catch. It is a defensible starting point, not a final answer — `STATUS.md`
Next-3 #1's labelled corpus removes the bias.
"""
from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger("sift-api.story_matcher")

# Cosine similarity floor for a pair to be worth an LLM opinion. See module
# docstring for the calibration and its known bias.
SIMILARITY_THRESHOLD = 0.60

# Neighbours fetched per new article. Beyond ~10 the tail is noise at this
# threshold, and every extra candidate is prompt tokens later.
TOP_K = 10

# Safety cap on a single run's queue. A backlog (first run after deploy, or
# after an outage) should drain over several cycles rather than build one
# enormous prompt. At ~40 new articles per 30-min cycle this is never hit.
MAX_QUEUE = 200

RECENCY_WINDOW_HOURS = 48


# Neighbours between this and SIMILARITY_THRESHOLD are recorded but not acted
# on. They exist to answer "is 0.60 leaving matches on the table?" — a question
# the threshold calibration cannot answer about itself, because its 283 pairs
# were only ever found inside a ~3.3h window and so under-represent exactly the
# long-range matches this design targets. Without this band, a threshold set
# too high looks identical to a corpus with nothing to find.
NEAR_MISS_FLOOR = 0.50


class Candidate(TypedDict):
    """One new article and the neighbours worth asking the LLM about."""

    article: dict
    # Existing stories a strong neighbour already belongs to:
    # {story_id: [neighbour dict, ...]}. Attaching preserves the story's id,
    # which is what stops the orphan churn.
    existing_stories: dict[str, list[dict]]
    # Strong neighbours with no story yet — a potential new cluster.
    loose_neighbours: list[dict]
    # NEAR_MISS_FLOOR <= similarity < threshold. Observation only.
    near_misses: list[dict]
    # Unique outlets across the article and its strong neighbours. A new
    # cluster needs >= 2 to survive the gate in incremental_threading, so this
    # predicts gate survival before any LLM call is made. Irrelevant to the
    # attach case, where the story already exists.
    unique_outlets: int


async def fetch_queue(pool, limit: int = MAX_QUEUE) -> list[dict]:
    """Articles waiting to be threaded, oldest first.

    `threaded_at IS NULL` is the queue. The window bound is what makes the
    ~280k historical rows (all NULL, never backfilled) invisible here — they
    fall outside it and age further out, never in.

    The entities filter is the same one the old fetch node used. Because this
    is a per-row marker rather than a watermark, an article whose entities have
    not landed yet is simply not selected *this* run and stays queued, instead
    of being skipped permanently.
    """
    rows = await pool.fetch(
        f"""
        SELECT id, source_url, source_name, title, summary, image_url,
               published_date, category, story_id, entities
        FROM articles
        WHERE threaded_at IS NULL
          AND from_search = false
          AND published_date > NOW() - INTERVAL '{RECENCY_WINDOW_HOURS} hours'
          AND embedding IS NOT NULL
          AND jsonb_typeof(entities) = 'object'
        ORDER BY published_date ASC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def find_candidates(
    pool,
    queue: list[dict],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
    top_k: int = TOP_K,
) -> list[Candidate]:
    """For each queued article, find neighbours worth an LLM opinion.

    Costs nothing but Postgres time. Articles with no neighbour above the
    threshold are returned with both collections empty — the caller still marks
    them threaded, so they park without being re-queued.
    """
    out: list[Candidate] = []
    for article in queue:
        rows = await pool.fetch(
            f"""
            SELECT id, source_url, source_name, title, summary, story_id,
                   published_date,
                   1 - (embedding <=> (SELECT embedding FROM articles WHERE id = $1))
                       AS similarity
            FROM articles
            WHERE category = $2
              AND id <> $1
              AND from_search = false
              AND published_date > NOW() - INTERVAL '{RECENCY_WINDOW_HOURS} hours'
              AND embedding IS NOT NULL
            ORDER BY embedding <=> (SELECT embedding FROM articles WHERE id = $1)
            LIMIT $3
            """,
            article["id"], article["category"], top_k,
        )

        existing: dict[str, list[dict]] = {}
        loose: list[dict] = []
        near: list[dict] = []
        for r in rows:
            sim = r["similarity"] or 0
            if sim < NEAR_MISS_FLOOR:
                # Ordered by distance, so everything after this is worse.
                break
            n = dict(r)
            if sim < threshold:
                near.append(n)
            elif n["story_id"]:
                existing.setdefault(n["story_id"], []).append(n)
            else:
                loose.append(n)

        strong = [*[x for v in existing.values() for x in v], *loose]
        outlets = {article.get("source_name")} | {
            n.get("source_name") for n in strong
        }
        out.append(Candidate(
            article=article,
            existing_stories=existing,
            loose_neighbours=loose,
            near_misses=near,
            unique_outlets=len([o for o in outlets if o]),
        ))
    return out


def summarize(candidates: list[Candidate]) -> dict:
    """Counts for the structured run event, and for shadow-mode comparison.

    Candidate counts are an UPPER BOUND on grouping, not a prediction of it.
    Two filters sit between them and a grouped article, and neither runs here:
    the confirmer rejects same-topic-different-event, and a new cluster needs
    >= 2 unique outlets. `would_survive_outlet_gate` closes the second gap for
    free; only the confirmer's judgement is still unmeasured, which is what
    the dry run in `shadow_report` exists for.
    """
    attach = sum(1 for c in candidates if c["existing_stories"])
    new_cluster = sum(
        1 for c in candidates
        if not c["existing_stories"] and c["loose_neighbours"]
    )
    parked = sum(
        1 for c in candidates
        if not c["existing_stories"] and not c["loose_neighbours"]
    )
    # Of the new-cluster candidates, how many bring enough outlet diversity to
    # survive MIN_UNIQUE_OUTLETS. Attach candidates are exempt — their story
    # already exists and already passed the gate.
    survivable = sum(
        1 for c in candidates
        if not c["existing_stories"] and c["loose_neighbours"]
        and c.get("unique_outlets", 0) >= 2
    )
    # Articles with nothing above the threshold but something in the near-miss
    # band. A large number here means 0.60 may be too strict.
    near_only = sum(
        1 for c in candidates
        if not c["existing_stories"] and not c["loose_neighbours"]
        and c.get("near_misses")
    )
    return {
        "queued": len(candidates),
        "attach_candidates": attach,
        "new_cluster_candidates": new_cluster,
        "new_clusters_passing_outlet_gate": survivable,
        "parked": parked,
        "parked_with_near_miss": near_only,
        "near_miss_floor": NEAR_MISS_FLOOR,
        # The share needing any LLM opinion at all. Everything else is free.
        "llm_relevant": attach + new_cluster,
    }


async def shadow_report(pool, *, confirm=None) -> dict:
    """Run the candidate step read-only and report what it *would* group.

    Writes nothing and marks nothing threaded. This is how the cutover bar in
    docs/INCREMENTAL_THREADING.md gets evidence instead of a projection — the
    repo's own standard (STATUS.md:21) is that a detector never run against a
    known-true case is an untested detector.

    Candidate counts alone are an UPPER BOUND, not a prediction: the confirmer
    rejects same-topic-different-event, and a new cluster needs >= 2 outlets.
    `summarize` now measures the outlet gate for free. Pass `confirm` to also
    exercise the LLM judgement — it runs the real confirmation call and
    reports what it decided, still without writing anything. That costs about
    $0.005 per run and is the only part of the cutover evidence that cannot be
    gathered for nothing.
    """
    queue = await fetch_queue(pool)
    if not queue:
        return {"event": "incremental_threading_shadow", "queued": 0}

    candidates = await find_candidates(pool, queue)
    report = {"event": "incremental_threading_shadow", **summarize(candidates)}

    if confirm is not None:
        relevant = [
            c for c in candidates
            if c["existing_stories"] or c["loose_neighbours"]
        ]
        if relevant:
            decisions = await confirm(relevant)
            actions = {"attach": 0, "new": 0, "none": 0}
            for d in decisions.values():
                actions[d.get("action", "none")] = actions.get(d.get("action", "none"), 0) + 1
            report["dry_run"] = actions
            # The number the cutover bar actually needs: confirmed groupings,
            # after both filters, against the live path's grouped count.
            report["would_group"] = actions["attach"] + actions["new"]
            report["confirm_rate"] = round(
                report["would_group"] / len(relevant), 3
            )

    # Per-category, because the live path's failure is category-shaped: sports
    # groups at 0.9% and energy at 30%, purely because LIMIT 50 collapses the
    # window differently at different volumes.
    by_cat: dict[str, int] = {}
    for c in candidates:
        if c["existing_stories"] or c["loose_neighbours"]:
            cat = c["article"].get("category") or "unknown"
            by_cat[cat] = by_cat.get(cat, 0) + 1
    report["llm_relevant_by_category"] = by_cat
    report["threshold"] = SIMILARITY_THRESHOLD
    return report


async def mark_threaded(pool, article_ids: list[str]) -> None:
    """Mark articles as considered, whatever the outcome.

    Parked singletons are marked too. They remain searchable as neighbours, so
    a later arrival can still pull them into a story — they just stop being
    re-queued, which is the whole point.
    """
    if not article_ids:
        return
    await pool.execute(
        "UPDATE articles SET threaded_at = NOW() WHERE id = ANY($1::text[])",
        article_ids,
    )
