"""Feed-balance drift tripwire — ranking v2 stage 3 (sift/docs/RANKING_SIGNALS.md).

The D48/D45 ranking work encoded editorial policy in formulas: a grim
dampener, a per-source cap, a saturating corroboration curve, a civic-density
boost. Every one of those has failure modes that would read as a vibe ("the
feed feels doom-y again") long before anyone opens the code. This module
makes the two policy-bearing numbers logged metrics with a baseline:

    grim_share_top10   share of the top-10 ranked pool articles tagged grim
    mean_civic_top10   mean (capped) civic-link weight of those articles

plus two recorded-but-untripped story metrics (mean sources and grim share
of the top-5 stories) for the stage-1 saturation change.

The ranked pool is computed with the SAME expression the read path uses
(sift/lib/db.ts: importance x decay x grim dampener x civic boost) — this
module measures the policy as deployed, not a private approximation. Keep
the SQL in lockstep with db.ts and scripts/explain_feed_queries.py.

Drift rule: today's value vs the trailing mean of the prior snapshots in the
last BASELINE_DAYS, tripping on an ABSOLUTE delta (|today - baseline| >
threshold). Ratios were rejected: grim share is bounded [0,1] and its
baseline can legitimately sit near 0, where any ratio explodes. Nothing
trips until MIN_BASELINE snapshots exist — the first days after deploy are
baseline-building, not alarm-worthy.

Snapshots persist in `feed_balance` (migrations/022) rather than only the
log stream, for the same reason threading_shadow does: Railway's log buffer
rotates and resets on deploy, so a trailing-13-day baseline could not be
reconstructed from logs.

Daily, not per-pipeline-run: drift is a question about days (the baseline
window is 13 days), and asking every 30 minutes produces 48 near-identical
answers — the same argument as feed_health's monitor.
"""
from __future__ import annotations

import json
import logging
from typing import NamedTuple

logger = logging.getLogger("sift-api.feed_balance")

# Categories worth a baseline: 'top' is the product's front page; politics
# and world are where the civic-density signal actually lives.
CATEGORIES = ["top", "politics", "world"]

TOP_N_ARTICLES = 10
TOP_N_STORIES = 5

# Baseline = mean of prior snapshots within this trailing window.
BASELINE_DAYS = 13
# No verdicts until this many prior snapshots exist.
MIN_BASELINE = 5

# Absolute-delta trip thresholds. grim_share is a [0,1] share: 0.25 is
# "a quarter of the top 10 changed tone-class vs normal". civic weight is
# a [0,3] scale: 0.75 is a quarter of its range.
GRIM_SHARE_DELTA = 0.25
CIVIC_WEIGHT_DELTA = 0.75

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# Offset from feed_health's 5-minute first check so the two daily reports
# don't interleave in the log.
FIRST_CHECK_DELAY_SECONDS = 10 * 60

# The ranked article pool, in lockstep with sift/lib/db.ts (clamped decay,
# grim dampener, capped civic boost — the source cap is irrelevant to a
# 10-row aggregate and omitted). Returns one row per category snapshot.
_ARTICLES_QUERY = """
WITH scored AS (
  SELECT tone, is_opinion,
         COALESCE((
           SELECT SUM(CASE t WHEN 'bill' THEN 1.0 WHEN 'politician' THEN 1.0 WHEN 'org' THEN 0.5 ELSE 0 END)
           FROM (SELECT DISTINCT el->>'type' AS t, el->>'canonical_id' AS cid
                 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(entity_links) = 'array' THEN entity_links ELSE '[]'::jsonb END) el) links
         ), 0) AS civic_weight,
         COALESCE(importance_score, 3)::float *
         EXP(-LEAST(GREATEST(EXTRACT(EPOCH FROM (NOW() - COALESCE(published_date, created_at))), 0) / 86400.0, 700)) *
         CASE WHEN tone = 'grim' AND COALESCE(importance_score, 3) <= 3 THEN 0.6 ELSE 1.0 END *
         (1 + 0.1 * LEAST(COALESCE((
           SELECT SUM(CASE t WHEN 'bill' THEN 1.0 WHEN 'politician' THEN 1.0 WHEN 'org' THEN 0.5 ELSE 0 END)
           FROM (SELECT DISTINCT el->>'type' AS t, el->>'canonical_id' AS cid
                 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(entity_links) = 'array' THEN entity_links ELSE '[]'::jsonb END) el) links
         ), 0), 3)) AS rank_score
  FROM articles
  WHERE category = $1 AND from_search = false
    AND summary IS NOT NULL AND summary != ''
    AND LOWER(summary) NOT LIKE 'unable to provide%'
    AND (published_date > NOW() - INTERVAL '30 days'
         OR (published_date IS NULL AND created_at > NOW() - INTERVAL '30 days'))
  ORDER BY rank_score DESC
  LIMIT $2
)
SELECT AVG(CASE WHEN tone = 'grim' THEN 1.0 ELSE 0 END) AS grim_share_top,
       AVG(LEAST(civic_weight, 3))                      AS mean_civic_top,
       AVG(CASE WHEN is_opinion THEN 1.0 ELSE 0 END)    AS opinion_share_top,
       COUNT(*)                                          AS n_articles
FROM scored
"""

# Top stories by the stage-1 saturating rank (lockstep with db.ts).
_STORIES_QUERY = """
WITH ranked AS (
  SELECT COUNT(a.id)::int AS sources,
         AVG(CASE WHEN a.tone = 'grim' THEN 1.0 ELSE 0 END) AS grim_share
  FROM stories s
  LEFT JOIN articles a
    ON a.story_id = s.id
    AND a.from_search = false
    AND a.summary IS NOT NULL AND a.summary != ''
    AND LOWER(a.summary) NOT LIKE 'unable to provide%'
  WHERE s.category = $1 AND s.synthesis_status = 'complete'
  GROUP BY s.id
  HAVING COUNT(a.id) >= 2
  ORDER BY
    (3 + 0.8 * LN(1 + COUNT(a.id)))::float *
    EXP(-LEAST(GREATEST(EXTRACT(EPOCH FROM (NOW() - COALESCE(s.published_date, s.created_at))), 0) / 86400.0, 700))
  DESC NULLS LAST
  LIMIT $2
)
SELECT AVG(sources)    AS mean_sources_top,
       AVG(grim_share) AS story_grim_share_top,
       COUNT(*)        AS n_stories
FROM ranked
"""

_HISTORY_QUERY = """
SELECT category, grim_share_top10, mean_civic_top10
  FROM feed_balance
 WHERE run_at > NOW() - make_interval(days => $1)
   AND run_at < date_trunc('day', NOW())
"""

_INSERT = """
INSERT INTO feed_balance
    (category, grim_share_top10, mean_civic_top10,
     mean_sources_top5, story_grim_share_top5, n_articles, n_stories,
     opinion_share_top10)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


class Drift(NamedTuple):
    category: str
    metric: str
    today: float
    baseline: float
    delta: float
    threshold: float


def evaluate_drift(
    today: dict[str, dict],
    history: list[dict],
) -> list[Drift]:
    """Compare today's per-category metrics against trailing baselines.

    Pure — no DB, no clock. `today` maps category -> snapshot dict;
    `history` is prior snapshot rows (excluding today). A category with
    fewer than MIN_BASELINE prior snapshots gets no verdict.
    """
    trips: list[Drift] = []
    for category, snap in today.items():
        prior = [h for h in history if h["category"] == category]
        if len(prior) < MIN_BASELINE:
            continue
        for metric, threshold in (
            ("grim_share_top10", GRIM_SHARE_DELTA),
            ("mean_civic_top10", CIVIC_WEIGHT_DELTA),
        ):
            value = snap.get(metric)
            baseline_values = [float(h[metric]) for h in prior if h[metric] is not None]
            if value is None or not baseline_values:
                continue
            baseline = sum(baseline_values) / len(baseline_values)
            delta = float(value) - baseline
            if abs(delta) > threshold:
                trips.append(Drift(
                    category, metric,
                    round(float(value), 3), round(baseline, 3),
                    round(delta, 3), threshold,
                ))
    return trips


async def check_feed_balance(pool) -> dict:
    """Snapshot the ranked-pool balance metrics, persist, evaluate drift.

    Emits one `feed_balance` event (always) and a human-readable warning
    per tripped metric. Returns the payload.
    """
    today: dict[str, dict] = {}
    for category in CATEGORIES:
        art = await pool.fetchrow(_ARTICLES_QUERY, category, TOP_N_ARTICLES)
        sto = await pool.fetchrow(_STORIES_QUERY, category, TOP_N_STORIES)
        snap = {
            "grim_share_top10": float(art["grim_share_top"]) if art and art["grim_share_top"] is not None else None,
            "mean_civic_top10": float(art["mean_civic_top"]) if art and art["mean_civic_top"] is not None else None,
            "opinion_share_top10": float(art["opinion_share_top"]) if art and art["opinion_share_top"] is not None else None,
            "mean_sources_top5": float(sto["mean_sources_top"]) if sto and sto["mean_sources_top"] is not None else None,
            "story_grim_share_top5": float(sto["story_grim_share_top"]) if sto and sto["story_grim_share_top"] is not None else None,
            "n_articles": int(art["n_articles"]) if art else 0,
            "n_stories": int(sto["n_stories"]) if sto else 0,
        }
        today[category] = snap
        try:
            await pool.execute(
                _INSERT, category,
                snap["grim_share_top10"], snap["mean_civic_top10"],
                snap["mean_sources_top5"], snap["story_grim_share_top5"],
                snap["n_articles"], snap["n_stories"],
                snap["opinion_share_top10"],
            )
        except Exception as e:  # noqa: BLE001 — observation must never break the monitor
            logger.warning("feed_balance snapshot insert failed for %s: %s", category, e)

    history = [dict(r) for r in await pool.fetch(_HISTORY_QUERY, BASELINE_DAYS)]
    trips = evaluate_drift(today, history)

    payload = {
        "event": "feed_balance",
        "baseline_days": BASELINE_DAYS,
        "categories": today,
        "tripped": [t._asdict() for t in trips],
    }
    logger.info(json.dumps(payload))
    for t in trips:
        # Human-readable too, so it lands without log parsing.
        logger.warning(
            "feed balance drift: %s %s is %.3f vs %.3f baseline (delta %+.3f, threshold %.2f)",
            t.category, t.metric, t.today, t.baseline, t.delta, t.threshold,
        )
    return payload


async def run_feed_balance_monitor() -> None:
    """Daily loop, mirroring feed_health's monitor."""
    import asyncio

    from app.db import get_pool

    logger.info("Feed balance monitor started (every %ds)", CHECK_INTERVAL_SECONDS)
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            await check_feed_balance(await get_pool())
        except asyncio.CancelledError:
            logger.info("Feed balance monitor cancelled")
            raise
        except Exception as e:
            logger.error("Feed balance check failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
