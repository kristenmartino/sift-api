from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

# Deliberately no `settings` import. This module records what things cost; it
# has no opinion about whether spending is allowed. That separation is the
# point — see _record_to_ledger.

logger = logging.getLogger("sift-api.usage")

# Claude Haiku 4.5 pricing (USD per 1M tokens)
# Source: https://docs.anthropic.com/en/docs/about-claude/pricing
#
# These stay module constants because two sibling repos are pinned to them:
# sift/lib/usage-tracker.ts and sift-mcp/src/sift_mcp/usage.py carry the same
# five numbers, and a golden cost of 7.38 is asserted against them in both
# sift-api/tests/test_usage_tracker.py and sift/__tests__/usage-tracker.test.ts.
# One number, three repos, two languages, no shared source — so moving them
# would break a test only sift's CI runs. PRICES below pins itself to them.
PRICE_INPUT_PER_M = 1.0
PRICE_OUTPUT_PER_M = 5.0
PRICE_CACHE_WRITE_5M_PER_M = 1.25  # 1.25x base input for 5-min ephemeral cache writes
PRICE_CACHE_READ_PER_M = 0.10  # 0.1x base input for cache hits

# Web search tool pricing: $10 per 1,000 searches
PRICE_WEB_SEARCH_PER_CALL = 0.010


@dataclass(frozen=True)
class ModelPrices:
    """USD per 1M tokens for one model."""

    input_per_m: float
    output_per_m: float
    cache_write_per_m: float
    cache_read_per_m: float


# Priced per model, because `log_usage` took a `model` argument and ignored it.
#
# Everything on the pipeline is Haiku, so that read as harmless — but
# `services/judge.py` runs Sonnet, and every judge call has been costed at
# Haiku's $1/$5 instead of Sonnet's $3/$15: understated ~3x. Nothing in prod
# spends it today (`why_it_matters_judge_enabled` defaults false), but every
# eval script that uses the judge under-reports its own cost, and the moment a
# second model runs anywhere the ledger starts recording fiction that
# scripts/verify_cost_baseline.py then reports as fact.
#
# An unknown model falls back to Haiku's rates rather than to zero: a wrong
# number is visible and gets corrected, a zero looks like "this stage is free".
PRICES: dict[str, ModelPrices] = {
    "claude-haiku-4-5": ModelPrices(1.0, 5.0, 1.25, 0.10),
    "claude-haiku-4-5-20251001": ModelPrices(1.0, 5.0, 1.25, 0.10),
    "claude-sonnet-4-6": ModelPrices(3.0, 15.0, 3.75, 0.30),
}

DEFAULT_MODEL = "claude-haiku-4-5"

# Unknown model ids we have already warned about, so the warning fires once per
# id per process rather than once per call.
_warned_models: set[str] = set()


def prices_for(model: str) -> ModelPrices:
    """Rates for `model`, falling back to the default model's rates.

    The fallback is logged once per unknown id: silently pricing an unrecognized
    model at Haiku rates is how the judge bug survived, so the mis-pricing has
    to announce itself.
    """
    found = PRICES.get(model or "")
    if found is not None:
        return found
    if model and model not in _warned_models:
        _warned_models.add(model)
        logger.warning(
            "usage: no price row for model %r — costing it at %s rates; "
            "add it to usage_tracker.PRICES",
            model,
            DEFAULT_MODEL,
        )
    return PRICES[DEFAULT_MODEL]


# Voyage AI voyage-3-lite embeddings (USD per 1M tokens). Voyage bills a small
# per-token rate above a generous free monthly tier; this is a conservative
# upper-bound estimate, used only for the daily cost ledger.
PRICE_VOYAGE_PER_M = 0.02

# The Message Batches API bills at 50% of standard rates.
# https://docs.anthropic.com/en/docs/build-with-claude/batch-processing
BATCH_DISCOUNT = 0.5


def log_usage(
    operation: str,
    response: Any,
    model: str = "claude-haiku-4-5",
    web_searches: int = 0,
) -> dict:
    """
    Log token usage + estimated cost from an Anthropic response as structured JSON.

    Args:
        operation: short identifier for the call site (e.g. "summarizer.batch")
        response: the anthropic.types.Message returned by messages.create
        model: model id used for the call (for breakdown/filtering)
        web_searches: number of web_search_20250305 tool invocations to attribute to this call

    Returns:
        The dict that was logged (useful for tests / aggregation).
    """
    try:
        usage = getattr(response, "usage", None)

        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage else 0

        p = prices_for(model)
        cost_usd = (
            (input_tokens * p.input_per_m / 1_000_000)
            + (output_tokens * p.output_per_m / 1_000_000)
            + (cache_creation * p.cache_write_per_m / 1_000_000)
            + (cache_read * p.cache_read_per_m / 1_000_000)
            + (web_searches * PRICE_WEB_SEARCH_PER_CALL)
        )

        payload = {
            "event": "api_usage",
            "operation": operation,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "web_searches": web_searches,
            "cost_usd": round(cost_usd, 6),
        }
        logger.info(json.dumps(payload))
        _record_to_ledger(
            operation,
            model,
            round(cost_usd, 6),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_creation,
            web_search_calls=web_searches,
        )
        return payload
    except Exception as e:
        # Never let telemetry break the pipeline
        logger.debug("usage logging failed for %s: %s", operation, e)
        return {}


def log_batch_usage(
    operation: str,
    results: list[dict],
    model: str = "claude-haiku-4-5",
) -> dict:
    """Log token usage + estimated cost for a Message Batches result set.

    The three batch paths (context, primer, entity extraction) recorded nothing
    at all until 2026-08-05, so their spend was invisible: the ledger totalled
    ~$8.99/day against a real bill of ~$10/day, and the ~$1/day gap was them.

    Two reasons this cannot just call `log_usage` per result:

    1. **Shape.** Batch results arrive as parsed JSONL dicts
       (`{"custom_id", "result": {"type", "message"}}`), not `anthropic.types.Message`
       objects, so the `getattr` access in `log_usage` reads nothing and would
       silently record a $0 cost — worse than not recording, because it looks
       like data.
    2. **Price.** The Batch API bills at 50% of standard rates. Charging batch
       tokens at list price would overstate this spend by 2x.

    Aggregates the whole set into one log line and one ledger row: per-request
    granularity is noise here, since a sub-batch shares one operation.
    Errored results carry no usage and are counted separately.
    """
    try:
        input_tokens = output_tokens = cache_read = cache_creation = 0
        ok = errored = 0

        for entry in results or []:
            result = (entry or {}).get("result") or {}
            if result.get("type") != "succeeded":
                errored += 1
                continue
            ok += 1
            usage = ((result.get("message") or {}).get("usage")) or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            cache_creation += int(usage.get("cache_creation_input_tokens") or 0)

        p = prices_for(model)
        cost_usd = BATCH_DISCOUNT * (
            (input_tokens * p.input_per_m / 1_000_000)
            + (output_tokens * p.output_per_m / 1_000_000)
            + (cache_creation * p.cache_write_per_m / 1_000_000)
            + (cache_read * p.cache_read_per_m / 1_000_000)
        )

        payload = {
            "event": "api_usage",
            "operation": operation,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "web_searches": 0,
            "batch": True,
            "requests_succeeded": ok,
            "requests_errored": errored,
            "cost_usd": round(cost_usd, 6),
        }
        logger.info(json.dumps(payload))
        _record_to_ledger(
            operation,
            model,
            round(cost_usd, 6),
            call_count=ok,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_creation,
        )
        return payload
    except Exception as e:
        logger.debug("batch usage logging failed for %s: %s", operation, e)
        return {}


def count_web_searches(response: Any) -> int:
    """Count server_tool_use blocks for web_search in an Anthropic response."""
    try:
        count = 0
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "server_tool_use":
                name = getattr(block, "name", "")
                if name == "web_search":
                    count += 1
        return count
    except Exception:
        return 0


# Fire-and-forget tasks that persist usage to the daily cost ledger. We keep a
# reference so the running loop doesn't garbage-collect them mid-flight.
_pending_records: set = set()


def voyage_cost(total_tokens: int) -> float:
    """Estimated USD cost for a Voyage embedding call, for the daily ledger."""
    return (total_tokens or 0) * PRICE_VOYAGE_PER_M / 1_000_000


def _record_to_ledger(
    operation: str,
    model: str,
    cost_usd: float,
    call_count: int = 1,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    web_search_calls: int = 0,
) -> None:
    """Best-effort: persist a Claude call's cost to the daily ledger without
    blocking the caller. No-op when there's no running event loop (sync
    contexts / unit tests).

    RECORDING IS DECOUPLED FROM ENFORCEMENT, deliberately. This function used
    to return early unless `ai_cost_guard_enabled`, which meant the flag that
    turns on *blocking* also turned on *measuring* — so with the default
    `false`, `ai_usage_daily` was never written and nobody could see it was
    empty. STATUS.md carried "~$15/mo" for weeks while the real figure was
    ~$300/mo, and the error was only caught by looking at the Anthropic bill.

    Wanting the ledger without the ceiling is the normal case, and it is what
    you need *before* setting a sensible ceiling. `cost_guard.check_budget`
    still honours `ai_cost_guard_enabled` for the blocking decision; this only
    ensures the numbers exist to make that decision with.
    """
    if cost_usd <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop to schedule onto
    try:
        from services.cost_guard import record_usage

        task = loop.create_task(
            record_usage(
                "anthropic",
                model,
                operation,
                cost_usd,
                call_count=call_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                web_search_calls=web_search_calls,
            )
        )
        _pending_records.add(task)
        task.add_done_callback(_pending_records.discard)
    except Exception as e:  # never let telemetry break the caller
        logger.debug("usage ledger scheduling failed for %s: %s", operation, e)


def log_output_stop(
    operation: str,
    response: Any,
    *,
    aligned: bool,
    batch_size: int,
    model: str = "",
) -> None:
    """Record how a call ended, without blocking the caller.

    Same fire-and-forget shape as `_record_to_ledger`: schedules onto the
    running loop, no-ops when there isn't one (sync contexts / unit tests),
    and never raises.

    `stop_reason == "max_tokens"` on a JSON-returning call means the response
    was cut mid-array — which is a parse failure downstream, not a partial
    result. Recording it next to `aligned` is what makes the correlation
    readable; see migrations/021.
    """
    try:
        usage = getattr(response, "usage", None)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        stop_reason = getattr(response, "stop_reason", None) or "unknown"
        # Read from the response, not from what we asked for: the response says
        # which model actually served the call, so a provider that silently
        # routes elsewhere shows up here instead of being recorded as the model
        # we intended. Falls back to the caller's value, then to '' — a blank is
        # honest about not knowing, a guess is not.
        served_model = str(getattr(response, "model", "") or model or "")

        loop = asyncio.get_running_loop()
        from services.cost_guard import record_output_stop

        task = loop.create_task(
            record_output_stop(
                operation,
                stop_reason,
                aligned=aligned,
                batch_size=batch_size,
                output_tokens=output_tokens,
                model=served_model,
            )
        )
        _pending_records.add(task)
        task.add_done_callback(_pending_records.discard)
    except RuntimeError:
        return  # no event loop to schedule onto
    except Exception as e:  # never let telemetry break the caller
        logger.debug("output-stop scheduling failed for %s: %s", operation, e)
