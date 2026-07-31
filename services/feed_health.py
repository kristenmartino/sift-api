"""Notice when a configured outlet stops contributing (#125).

`feed_stats` in services/rss.py answers "did the fetch work" once per run. This
answers "is this outlet still producing", which is a question about days and
therefore a different query against a different table.

The distinction matters because `feed_stats.articles_by_source` counts articles
**fetched**, not articles **new** — dedup runs later, in `deduplicate_node`. A
feed serving the same ten items forever reports as perfectly healthy there. WHO
does exactly that today: 10 fetched every run, new rows only every few days.

So the signal lives in `articles.created_at`, and the rule has to tell apart
two things a row count cannot:

    dead        no new rows, and it used to produce regularly
    low-volume  no new rows, and it never produced regularly

Over 14 days of prod the healthy outlets span 21 to 4,203 rows, so no flat
threshold works — on volume OR on active days. #125 first proposed "flag if
silent 3 days and active on >= 4 of 14", which the tests immediately caught:
WHO is active on 5 days of 14, so that rule pages on WHO behaving normally.

The measurement that actually separates them is each outlet's OWN publishing
rhythm. An outlet active on N of the window's days has a typical gap of
WINDOW_DAYS / N, and it is only interesting when it has been silent for
several times that:

    outlet       active/14   typical gap   leash    real silence   verdict
    NPR              14         1.0 d      3.0 d       0.1 d       ok
    ProPublica       12         1.2 d      3.5 d       5.0 d       STALLED
    WHO               5         2.8 d      8.4 d       4.0 d       quiet
    Washington Post  14         1.0 d      3.0 d      15.4 d       STALLED

A low-volume outlet earns a proportionally longer leash, which is the whole
point: WHO's silence is normal, ProPublica's identical silence is not.

USA Today — in FEEDS for its entire life without ever producing an article
(#122) — is the separate, louder case: no rhythm to break, and no window needs
to elapse before that is obviously wrong.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import NamedTuple

logger = logging.getLogger("sift-api.feed_health")

# Trailing window used to measure each outlet's own publishing rhythm.
WINDOW_DAYS = 14

# Nothing is ever flagged before this much silence, however brisk the outlet.
# Three days clears a weekend plus a quiet Monday.
STALE_FLOOR_DAYS = 3.0

# Silence worth this many of an outlet's own typical gaps is the trigger. At 3,
# a daily outlet is flagged after 3 days and WHO (a gap of ~2.8 days) after
# ~8.4 — each judged against its own normal rather than a shared one.
GAP_MULTIPLIER = 3.0

# Nobody gets a longer leash than the window itself: producing nothing across
# 14 days is dead by any reading, including for an outlet whose measured gap
# would otherwise excuse it.
MAX_SILENCE_DAYS = float(WINDOW_DAYS)

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
FIRST_CHECK_DELAY_SECONDS = 5 * 60  # let a deploy settle, then report early

# `active_days` is measured over the window ending at the outlet's LAST new
# row — its rhythm while it was working — not the window ending now.
#
# Measuring to now makes the leash drift: a feed dead long enough has no recent
# activity left in the window, so its leash widens toward the cap and the
# verdict can soften as the outage lengthens. Replaying Washington Post day by
# day against prod showed exactly that (stalled 07-20, back to "quiet" 07-27
# and 07-28). Anchoring the window to the last row makes the leash a fixed
# property of the outlet, so only `days_since` moves and the verdict can only
# harden.
_QUERY = """
WITH last_row AS (
    SELECT source_name, MAX(created_at) AS last_new_row
      FROM articles
     WHERE from_search = false
     GROUP BY source_name
)
SELECT l.source_name,
       l.last_new_row,
       COUNT(DISTINCT date_trunc('day', a.created_at)) AS active_days
  FROM last_row l
  LEFT JOIN articles a
         ON a.source_name = l.source_name
        AND a.from_search = false
        AND a.created_at >  l.last_new_row - make_interval(days => $1)
        AND a.created_at <= l.last_new_row
 GROUP BY 1, 2
"""


class OutletHealth(NamedTuple):
    source_name: str
    verdict: str  # "ok" | "stalled" | "quiet" | "never"
    days_since: float | None  # None when the outlet has never produced
    active_days: int
    leash_days: float | None = None  # how long this outlet's silence may run


def leash_for(active_days: int) -> float:
    """How many days of silence are normal for an outlet with this rhythm.

    Active on every day of the window -> the floor. Active on a handful ->
    proportionally longer, because that IS its normal. Capped at the window,
    so nothing earns an unbounded leash.
    """
    if active_days <= 0:
        return MAX_SILENCE_DAYS
    typical_gap = WINDOW_DAYS / active_days
    return min(MAX_SILENCE_DAYS, max(STALE_FLOOR_DAYS, GAP_MULTIPLIER * typical_gap))


def evaluate_feed_health(
    configured: list[str],
    rows: list[dict],
    now: datetime,
) -> list[OutletHealth]:
    """Classify every CONFIGURED outlet. Pure — no DB, no clock.

    `rows` may contain source_names that are no longer configured (prod holds
    125 of them, from outlets pruned in earlier phases). Those are ignored:
    an outlet we removed on purpose is not a fault.
    """
    by_source = {r["source_name"]: r for r in rows}
    out: list[OutletHealth] = []

    for source_name in configured:
        row = by_source.get(source_name)
        last = row["last_new_row"] if row else None
        active_days = int(row["active_days"] or 0) if row else 0

        if last is None:
            out.append(OutletHealth(source_name, "never", None, 0, None))
            continue

        days_since = (now - last).total_seconds() / 86400
        leash = leash_for(active_days)
        if active_days == 0:
            # Unreachable from _QUERY — an outlet with a last row is active on
            # at least the day of that row. Defensive: a zero here would
            # otherwise buy the widest possible leash, which is backwards.
            verdict = "stalled"
        elif days_since >= leash:
            verdict = "stalled"  # silent well past its own normal
        elif days_since >= STALE_FLOOR_DAYS:
            verdict = "quiet"  # unusual for a daily outlet, normal for this one
        else:
            verdict = "ok"
        out.append(
            OutletHealth(source_name, verdict, round(days_since, 1), active_days, round(leash, 1))
        )

    return out


def _summarize(results: list[OutletHealth]) -> dict:
    def pick(verdict: str) -> list[dict]:
        return [
            {
                "source": r.source_name,
                "days_since": r.days_since,
                "active_days": r.active_days,
                "leash_days": r.leash_days,
            }
            for r in results
            if r.verdict == verdict
        ]

    never, stalled, quiet = pick("never"), pick("stalled"), pick("quiet")
    return {
        "event": "feed_health",
        "window_days": WINDOW_DAYS,
        "outlets_total": len(results),
        "outlets_ok": sum(1 for r in results if r.verdict == "ok"),
        "never_ingested": never,
        "stalled": stalled,
        "quiet": quiet,
    }


async def check_feed_health(pool, now: datetime | None = None) -> dict:
    """Run the check and emit one `feed_health` event. Returns the payload."""
    from services.rss import FEEDS

    rows = await pool.fetch(_QUERY, WINDOW_DAYS)
    if now is None:
        now = await pool.fetchval("SELECT NOW()")

    configured = sorted({source_name for source_name, _ in FEEDS})
    results = evaluate_feed_health(configured, [dict(r) for r in rows], now)
    payload = _summarize(results)

    logger.info(json.dumps(payload))
    broken = payload["never_ingested"] + payload["stalled"]
    if broken:
        # Human-readable too, so it lands without log parsing.
        logger.warning(
            "%d outlet(s) have stopped contributing: %s",
            len(broken),
            ", ".join(
                f"{b['source']} ({'never' if b['days_since'] is None else str(b['days_since']) + 'd'})"
                for b in broken
            ),
        )
    return payload


async def run_feed_health_monitor() -> None:
    """Daily loop. Deliberately not per-run: this is a question about days, so
    asking it every 30 minutes produces 48 identical answers a day."""
    import asyncio

    from app.db import get_pool

    logger.info("Feed health monitor started (every %ds)", CHECK_INTERVAL_SECONDS)
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            await check_feed_health(await get_pool())
        except asyncio.CancelledError:
            logger.info("Feed health monitor cancelled")
            raise
        except Exception as e:
            logger.error("Feed health check failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def days_ago(now: datetime, days: float) -> datetime:
    """Test helper kept next to the rule it exercises."""
    return now - timedelta(days=days)
