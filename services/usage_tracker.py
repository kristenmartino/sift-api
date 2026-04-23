from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("sift-api.usage")

# Claude Haiku 4.5 pricing (USD per 1M tokens)
# Source: https://docs.anthropic.com/en/docs/about-claude/pricing
PRICE_INPUT_PER_M = 1.0
PRICE_OUTPUT_PER_M = 5.0
PRICE_CACHE_WRITE_5M_PER_M = 1.25  # 1.25x base input for 5-min ephemeral cache writes
PRICE_CACHE_READ_PER_M = 0.10  # 0.1x base input for cache hits

# Web search tool pricing: $10 per 1,000 searches
PRICE_WEB_SEARCH_PER_CALL = 0.010


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

        cost_usd = (
            (input_tokens * PRICE_INPUT_PER_M / 1_000_000)
            + (output_tokens * PRICE_OUTPUT_PER_M / 1_000_000)
            + (cache_creation * PRICE_CACHE_WRITE_5M_PER_M / 1_000_000)
            + (cache_read * PRICE_CACHE_READ_PER_M / 1_000_000)
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
        return payload
    except Exception as e:
        # Never let telemetry break the pipeline
        logger.debug("usage logging failed for %s: %s", operation, e)
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
