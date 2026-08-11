"""Daily compare example — the anonymous door into the compare feature.

Generates ONE real comparison per UTC day and stores it in
``daily_compare_example`` (migration 021). Signed-out visitors on /news and
the landing page's comparison section read it via sift/lib/db.ts — real,
dated output from the actual tool in place of a hand-written static demo.

Cost posture: at most one compare per day, pre-checked against the same
daily AI budget guard as the live endpoint, so the marketing surface can
never spend past the ceiling.

Trigger: fire-and-forget after each successful pipeline run (the pipeline
heartbeat guarantees ~30-minute cadence, so the example refreshes within
half an hour of the UTC day rolling over). A protected force-refresh
endpoint lives in app/routers/compare.py for first-deploy seeding and ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from app.db import get_pool
from services.cost_guard import check_budget
from workflows.compare_workflow import CompareState, build_compare_graph

logger = logging.getLogger("sift-api.daily-compare")

# Mirrors COMPARE_COST_ESTIMATE_PER_SOURCE_USD in app/routers/compare.py —
# deliberately high so the example is blocked before it would cross the
# ceiling, not after.
COST_ESTIMATE_PER_SOURCE_USD = 0.04

# A fixed cross-spectrum set, all present in the compare allowlist. Fixed on
# purpose: the example demonstrates the feature's spread, and a stable set
# keeps day-over-day output comparable.
DAILY_COMPARE_SOURCES = [
    "Reuters",
    "Associated Press",
    "Fox News",
    "The New York Times",
    "BBC",
]

# The daily job has no Vercel proxy chain above it, so it can wait longer
# than the live endpoint's 50s ceiling.
DAILY_COMPARE_TIMEOUT = 90  # seconds

_graph = build_compare_graph()
_refresh_lock = asyncio.Lock()


async def _pick_topic(conn) -> str | None:
    """Today's most important non-grim story title.

    Non-grim preferred for the same reason the landing hero prefers it (D48):
    this is a marketing surface, and a mass-casualty headline as the standing
    example sets the wrong register. Grim still wins if it is all there is —
    honesty over comfort.
    """
    row = await conn.fetchrow(
        """
        SELECT title FROM articles
        WHERE published_date >= NOW() - INTERVAL '1 day'
          AND summary IS NOT NULL AND summary != ''
          AND from_search = false
        ORDER BY (COALESCE(tone, 'neutral') = 'grim')::int ASC,
                 importance_score DESC NULLS LAST,
                 published_date DESC
        LIMIT 1
        """
    )
    if not row or not row["title"]:
        return None
    # Titles work as compare topics (the workflow web-searches them), but cap
    # the length so a run-on headline doesn't bloat five search prompts.
    return str(row["title"])[:120]


async def refresh_daily_example(force: bool = False) -> bool:
    """Refresh the stored example if it is stale. Returns True if refreshed.

    Never raises — callers fire-and-forget this after pipeline runs, and a
    failed example refresh must not fail (or even color) the pipeline.
    """
    if _refresh_lock.locked():
        logger.info("daily-compare: refresh already in flight, skipping")
        return False

    async with _refresh_lock:
        try:
            return await _refresh(force)
        except Exception:
            logger.exception("daily-compare: refresh failed")
            return False


async def _refresh(force: bool) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not force:
            row = await conn.fetchrow(
                "SELECT (generated_at AT TIME ZONE 'UTC')::date = "
                "(NOW() AT TIME ZONE 'UTC')::date AS fresh "
                "FROM daily_compare_example WHERE id = 1"
            )
            if row and row["fresh"]:
                return False

        topic = await _pick_topic(conn)
        if not topic:
            logger.warning("daily-compare: no candidate story today, skipping")
            return False

    budget = await check_budget(
        COST_ESTIMATE_PER_SOURCE_USD * len(DAILY_COMPARE_SOURCES)
    )
    if not budget.allowed:
        logger.warning(
            "daily-compare: blocked by cost guard (reason=%s)", budget.reason
        )
        return False

    logger.info("daily-compare: generating for topic %r", topic)
    start = time.time()

    initial_state: CompareState = {
        "topic": topic,
        "sources": list(DAILY_COMPARE_SOURCES),
        "search_results": {},
        "claims": [],
        "comparison": "",
        "errors": [],
    }
    result = await asyncio.wait_for(
        _graph.ainvoke(initial_state), timeout=DAILY_COMPARE_TIMEOUT
    )

    comparison = result.get("comparison", "")
    claims = result.get("claims", [])
    if not comparison and not claims:
        logger.warning("daily-compare: workflow produced no coverage, keeping old example")
        return False

    payload = {
        "topic": topic,
        "comparison": comparison,
        "sources_checked": list(result.get("search_results", {}).keys())
        or list(DAILY_COMPARE_SOURCES),
        "claims": claims,
        "duration_ms": int((time.time() - start) * 1000),
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO daily_compare_example (id, payload, generated_at)
            VALUES (1, $1::jsonb, NOW())
            ON CONFLICT (id) DO UPDATE
                SET payload = EXCLUDED.payload,
                    generated_at = EXCLUDED.generated_at
            """,
            json.dumps(payload),
        )

    logger.info(
        "daily-compare: stored example (%d claims, %.1fs)",
        len(claims),
        payload["duration_ms"] / 1000,
    )
    return True
