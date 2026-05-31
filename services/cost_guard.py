from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import sentry_sdk

from app.config import settings
from app.db import get_pool

logger = logging.getLogger("sift-api.cost_guard")

# In-process de-dup so the "80% of budget" alert fires at most once per UTC day
# per worker. Cheap and good enough; a restart may re-alert, which is harmless.
_alerted_dates: set[str] = set()


@dataclass(frozen=True)
class BudgetDecision:
    """Outcome of a check_budget() call."""

    allowed: bool
    reason: str
    spent_usd: float
    limit_usd: float


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


async def check_budget(estimated_cost_usd: float = 0.0) -> BudgetDecision:
    """Decide whether a paid AI call may proceed under today's budget.

    Returns ``allowed=True`` when the guard is disabled, when today's spend plus
    the estimated cost is within ``daily_ai_cost_limit_usd``, or when the ledger
    can't be read (fail-open: the guard must never become a new outage vector —
    a transient DB blip should not halt every paid call). Returns
    ``allowed=False`` only when the projected spend would exceed the limit;
    callers should then skip the provider call and degrade gracefully.
    """
    limit = settings.daily_ai_cost_limit_usd
    if not settings.ai_cost_guard_enabled:
        return BudgetDecision(True, "guard_disabled", 0.0, limit)

    try:
        pool = await get_pool()
        spent = await pool.fetchval(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) "
            "FROM ai_usage_daily WHERE usage_date = $1",
            _utc_today(),
        )
        spent = float(spent or 0.0)
    except Exception as e:
        logger.warning(
            "cost_guard: ledger read failed, allowing call (fail-open): %s", e
        )
        return BudgetDecision(True, "ledger_unavailable", 0.0, limit)

    projected = spent + max(0.0, estimated_cost_usd)
    if limit > 0 and projected > limit:
        logger.warning(
            "cost_guard: daily AI budget reached — blocking paid call "
            "(spent=$%.4f + est=$%.4f > limit=$%.2f)",
            spent,
            estimated_cost_usd,
            limit,
        )
        return BudgetDecision(False, "budget_exceeded", spent, limit)

    _maybe_alert(spent, limit)
    return BudgetDecision(True, "within_budget", spent, limit)


async def record_usage(
    provider: str,
    model: str,
    operation: str,
    cost_usd: float,
    call_count: int = 1,
) -> None:
    """Add a paid call's estimated cost to today's ledger row.

    No-op when the guard is disabled or there's nothing to record. Never raises:
    lost telemetry must not break the pipeline or a request.
    """
    if not settings.ai_cost_guard_enabled:
        return
    if cost_usd <= 0 and call_count <= 0:
        return

    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO ai_usage_daily
                (usage_date, provider, model, operation, estimated_cost_usd,
                 call_count, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (usage_date, provider, model, operation)
            DO UPDATE SET
                estimated_cost_usd =
                    ai_usage_daily.estimated_cost_usd + EXCLUDED.estimated_cost_usd,
                call_count = ai_usage_daily.call_count + EXCLUDED.call_count,
                updated_at = NOW()
            """,
            _utc_today(),
            provider,
            model,
            operation,
            float(cost_usd),
            int(call_count),
        )
    except Exception as e:
        logger.debug(
            "cost_guard: ledger write failed for %s/%s: %s", provider, operation, e
        )


def _maybe_alert(spent: float, limit: float) -> None:
    """Emit a single warning per UTC day when spend crosses the alert ratio.

    Always logs; also sends a Sentry message when Sentry is configured
    (SENTRY_DSN set). Falls back to logs-only when Sentry is inert.
    """
    if limit <= 0 or spent / limit < settings.ai_cost_alert_threshold_ratio:
        return

    today = _utc_today().isoformat()
    if today in _alerted_dates:
        return
    _alerted_dates.add(today)

    msg = (
        f"AI spend at {spent / limit:.0%} of the daily budget "
        f"(${spent:.4f} / ${limit:.2f}) on {today}"
    )
    logger.warning("cost_guard: %s", msg)
    if settings.sentry_dsn:
        try:
            sentry_sdk.capture_message(msg, level="warning")
        except Exception as e:
            logger.debug("cost_guard: sentry alert failed: %s", e)
