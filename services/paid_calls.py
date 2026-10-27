"""Shared paid-call admission. Uncertain provider costs remain held for reconciliation."""

from __future__ import annotations

import json
import logging
import anthropic
from contextvars import ContextVar

from app.config import settings
from services.cost_guard import reserve_budget, release_budget, record_usage
from services.usage_tracker import PRICES, log_usage, count_web_searches

budget_scope: ContextVar[str] = ContextVar("ai_budget_scope", default="background")
logger = logging.getLogger('sift-api.paid_calls')


class BudgetUnavailable(RuntimeError):
    def __init__(self, message: str, code: str = "AI_BUDGET_EXCEEDED"):
        super().__init__(message)
        self.code = code


def message_bound(params: dict, *, batch: bool = False) -> float:
    """Conservative text-only bound; web tools include each internal model pass.

    UTF-8 bytes bound tokenized text. Search has a 200k context limit and can
    re-bill input on tool iterations. Guarded clients disable automatic retries.
    """
    prices = PRICES.get(params.get("model", ""))
    if not prices or not params.get("max_tokens"):
        return float("inf")
    serialized = json.dumps({k: params[k] for k in ("messages", "system") if k in params}, ensure_ascii=False)
    inputs = len(serialized.encode("utf-8")) + 4096
    outputs = int(params["max_tokens"])
    searches = 0
    for tool in params.get("tools", []):
        if tool.get("name") != "web_search" or not tool.get("max_uses"):
            return float("inf")
        searches += int(tool["max_uses"])
    if searches:
        # Before the first tool call the input is the supplied prompt, already
        # bounded above. Only subsequent passes can include unknown search
        # results. Reserve a full context for EACH such pass, including the
        # original prompt again; do not reserve an imaginary initial 200k.
        # https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
        inputs += max(inputs, 200_000) * searches
        outputs *= searches + 1
    return (
        (inputs * max(prices.input_per_m, prices.cache_write_per_m) + outputs * prices.output_per_m)
        / 1_000_000
        + searches * 0.01
    ) * (0.5 if batch else 1)


async def persist_payload(
    payload: dict, model: str, operation: str, hold_id: str, *, call_count: int = 1
) -> bool:
    return await record_usage(
        "anthropic",
        model,
        operation,
        payload.get("cost_usd", 0),
        call_count=call_count,
        input_tokens=payload.get("input_tokens", 0),
        output_tokens=payload.get("output_tokens", 0),
        cache_read_tokens=payload.get("cache_read_input_tokens", 0),
        cache_write_tokens=payload.get("cache_creation_input_tokens", 0),
        web_search_calls=payload.get("web_searches", 0),
        reservation_id=hold_id or None,
    )


async def budgeted_message(client, operation: str, *, stream_response: bool = False, **params):
    async def create_response(provider):
        if stream_response:
            async with provider.messages.stream(**params) as stream:
                response = await stream.get_final_message()
                headers = getattr(getattr(stream, 'response', None), 'headers', {})
                request_id = headers.get('request-id') if hasattr(headers, 'get') else None
                if isinstance(request_id, str):
                    object.__setattr__(response, '_request_id', request_id)
                return response
        return await provider.messages.create(**params)

    if not settings.ai_cost_guard_enabled:
        return await create_response(client)
    bound = message_bound(params)
    hold_id = await reserve_budget(bound, scope=budget_scope.get())
    if hold_id is None:
        raise BudgetUnavailable("Paid call blocked by shared AI budget")
    audit = {'event': 'paid_call_reservation', 'hold_id': hold_id,
             'operation': operation, 'model': params['model'], 'scope': budget_scope.get()}
    logger.info(json.dumps({**audit, 'outcome': 'reserved', 'reserved_usd': bound}))
    # Unknown outcome (timeout/cancellation) intentionally retains its hold.
    try:
        response = await create_response(client.with_options(max_retries=0))
    except BaseException as exc:
        # Definitive rejection: no generation was accepted or billed.
        rejected = isinstance(exc, anthropic.APIStatusError) and exc.status_code in {400, 401, 403, 404, 413, 422, 429}
        if rejected:
            await release_budget(hold_id)
        logger.warning(json.dumps({**audit, 'outcome': 'released_rejection' if rejected else 'uncertain',
            'error_type': type(exc).__name__, 'status_code': getattr(exc, 'status_code', None),
            'request_id': getattr(exc, 'request_id', None)}))
        raise
    if getattr(response, "usage", None) is None:
        logger.error(json.dumps({**audit, 'outcome': 'held_missing_usage'}))
        raise BudgetUnavailable("Provider usage missing; budget remains held", "COST_GUARD_UNAVAILABLE")
    payload = log_usage(
        operation, response, model=params["model"], web_searches=count_web_searches(response), persist=False
    )
    if payload and await persist_payload(payload, params["model"], operation, hold_id):
        object.__setattr__(response, "_sift_usage_payload", payload)
        object.__setattr__(response, "_sift_usage_recorded", True)
        logger.info(json.dumps({**audit, 'outcome': 'settled', 'cost_usd': payload.get('cost_usd'),
                               'request_id': getattr(response, '_request_id', None)}))
    else:
        # Prevent legacy best-effort logging from settling this a second time.
        object.__setattr__(response, "_sift_usage_recorded", True)
        logger.error(json.dumps({**audit, 'outcome': 'held_accounting_unavailable'}))
        raise BudgetUnavailable("Paid response held: usage accounting unavailable", "COST_GUARD_UNAVAILABLE")
    return response
