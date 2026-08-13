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

    Returns ``allowed=True`` when the guard is disabled, or when today's spend
    plus the estimated cost is within ``daily_ai_cost_limit_usd``. Returns
    ``allowed=False`` when the projected spend would exceed the limit — and also
    when the guard is enabled but the ledger can't be read (fail-closed): if we
    can't verify spend we must not authorize paid calls, otherwise an enabled
    ceiling would permit unlimited spend during the exact failure mode where
    spend can't be measured. Callers should skip the provider call and degrade
    gracefully whenever ``allowed=False``.
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
        # Fail CLOSED: when the guard is enabled but the ledger can't be read we
        # can't verify spend, so we must not authorize paid calls. An enabled
        # ceiling that failed open would permit unlimited spend during the exact
        # failure mode where spend can't be measured.
        logger.error(
            "cost_guard: ledger read failed, blocking call (fail-closed): %s", e
        )
        return BudgetDecision(False, "guard_unavailable", 0.0, limit)

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
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    web_search_calls: int = 0,
) -> None:
    """Add a paid call's estimated cost and token counts to today's ledger row.

    Records unconditionally — `ai_cost_guard_enabled` gates *enforcement*
    (`check_budget`), not measurement. It used to gate this function too, which
    is why `ai_usage_daily` sat empty from its creation until 2026-07-30 while
    STATUS.md quoted a figure ~20x below the real spend. A ledger you have to
    opt into is a ledger nobody knows is missing, and you need it populated
    *before* you can pick a sensible ceiling.

    The token counts (migrations/029) are what make the dollars re-pricable. A
    stored cost cannot be projected onto another model's rates — one equation,
    two unknowns — so without these, comparing models means paying to find out.
    They are keyword-only and default to 0 so existing positional callers
    (services/embedder.py) keep working unchanged.

    Never raises: lost telemetry must not break the pipeline or a request.
    """
    if cost_usd <= 0 and call_count <= 0:
        return

    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO ai_usage_daily
                (usage_date, provider, model, operation, estimated_cost_usd,
                 call_count, input_tokens, output_tokens, cache_read_tokens,
                 cache_write_tokens, web_search_calls, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
            ON CONFLICT (usage_date, provider, model, operation)
            DO UPDATE SET
                estimated_cost_usd =
                    ai_usage_daily.estimated_cost_usd + EXCLUDED.estimated_cost_usd,
                call_count = ai_usage_daily.call_count + EXCLUDED.call_count,
                input_tokens =
                    ai_usage_daily.input_tokens + EXCLUDED.input_tokens,
                output_tokens =
                    ai_usage_daily.output_tokens + EXCLUDED.output_tokens,
                cache_read_tokens =
                    ai_usage_daily.cache_read_tokens + EXCLUDED.cache_read_tokens,
                cache_write_tokens =
                    ai_usage_daily.cache_write_tokens + EXCLUDED.cache_write_tokens,
                web_search_calls =
                    ai_usage_daily.web_search_calls + EXCLUDED.web_search_calls,
                updated_at = NOW()
            """,
            _utc_today(),
            provider,
            model,
            operation,
            float(cost_usd),
            int(call_count),
            int(input_tokens or 0),
            int(output_tokens or 0),
            int(cache_read_tokens or 0),
            int(cache_write_tokens or 0),
            int(web_search_calls or 0),
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


async def record_output_stop(
    operation: str,
    stop_reason: str,
    *,
    aligned: bool,
    batch_size: int,
    output_tokens: int,
    model: str = "",
) -> None:
    """Count how one Claude call ended, in `llm_output_stops` (migrations 021+029).

    Separate from `record_usage` on purpose: that table answers "what did we
    spend", this one answers "why are we re-asking". Splitting on `aligned` is
    the point — the question is not whether calls ever hit `max_tokens` but
    whether the *misaligned* ones are the ones that did.

    `model` joins the key in 029 because that split is also the only stored
    signal for whether a *candidate* model can produce parseable indexed JSON,
    and without it both arms of an A/B collide on one row. It defaults to ''
    rather than to a model name so a caller that cannot say stays honest about
    not knowing; rows written before 029 carry '' for the same reason.

    Never raises, for the same reason as everything else in this module: lost
    telemetry must not break ingest.
    """
    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO llm_output_stops
                (usage_date, operation, model, stop_reason, aligned, batch_size,
                 call_count, max_output_tokens, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 1, $7, NOW())
            ON CONFLICT (usage_date, operation, model, stop_reason, aligned,
                         batch_size)
            DO UPDATE SET
                call_count = llm_output_stops.call_count + 1,
                max_output_tokens = GREATEST(
                    llm_output_stops.max_output_tokens, EXCLUDED.max_output_tokens
                ),
                updated_at = NOW()
            """,
            _utc_today(),
            operation,
            str(model or ""),
            str(stop_reason or "unknown"),
            bool(aligned),
            int(batch_size),
            int(output_tokens or 0),
        )
    except Exception as e:
        logger.debug("cost_guard: output-stop write failed for %s: %s", operation, e)
