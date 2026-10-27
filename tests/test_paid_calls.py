from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services import paid_calls, usage_tracker

PARAMS = {"model": "claude-haiku-4-5", "max_tokens": 2048, "messages": [{"role": "user", "content": "test"}]}


def test_bounds_include_tool_iterations_and_batch_discount():
    plain = paid_calls.message_bound(PARAMS)
    search = paid_calls.message_bound({**PARAMS, "tools": [{"name": "web_search", "max_uses": 1}]})
    assert search > 0.5
    assert search * 5 + plain < 3  # Five outlet searches still fit the $3 reserve.
    assert paid_calls.message_bound(PARAMS, batch=True) == plain / 2
    assert paid_calls.message_bound({**PARAMS, "model": "unknown"}) == float("inf")
    assert paid_calls.message_bound({**PARAMS, "tools": [{"name": "unknown"}]}) == float("inf")


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(paid_calls.settings, "ai_cost_guard_enabled", True)
    reserve = AsyncMock(return_value="hold")
    persist = AsyncMock(return_value=True)
    release = AsyncMock()
    monkeypatch.setattr(paid_calls, "reserve_budget", reserve)
    monkeypatch.setattr(paid_calls, "persist_payload", persist)
    monkeypatch.setattr(paid_calls, "release_budget", release)
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=20), content=[])
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    client.with_options = Mock(return_value=client)
    return client, response, reserve, persist, release


@pytest.mark.asyncio
async def test_guarded_call_awaits_settlement_and_legacy_logging_does_not_duplicate(guarded, monkeypatch):
    client, response, reserve, persist, _ = guarded
    record = Mock()
    monkeypatch.setattr(usage_tracker, "_record_to_ledger", record)
    token = paid_calls.budget_scope.set("compare")
    try:
        assert await paid_calls.budgeted_message(client, "compare.search_sources", **PARAMS) is response
    finally:
        paid_calls.budget_scope.reset(token)
    assert reserve.await_args.kwargs["scope"] == "compare"
    persist.assert_awaited_once()
    client.with_options.assert_called_once_with(max_retries=0)
    usage_tracker.log_usage("compare.search_sources", response)
    record.assert_not_called()


@pytest.mark.asyncio
async def test_denied_admission_makes_no_provider_call(guarded):
    client, _, reserve, persist, _ = guarded
    reserve.return_value = None
    with pytest.raises(paid_calls.BudgetUnavailable):
        await paid_calls.budgeted_message(client, "judge.batch", **PARAMS)
    client.messages.create.assert_not_awaited()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_retains_uncertain_hold(guarded):
    client, _, _, persist, release = guarded
    client.messages.create.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await paid_calls.budgeted_message(client, "judge.batch", **PARAMS)
    release.assert_not_awaited()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_ledger_failure_retains_hold_and_does_not_return_unaccounted_result(guarded):
    client, _, _, persist, release = guarded
    persist.return_value = False
    with pytest.raises(paid_calls.BudgetUnavailable, match="accounting unavailable"):
        await paid_calls.budgeted_message(client, "judge.batch", **PARAMS)
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_definitive_provider_rejection_releases_unspent_hold(guarded):
    import anthropic
    import httpx

    client, _, _, persist, release = guarded
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    client.messages.create.side_effect = anthropic.RateLimitError("rate limited", response=response, body={})
    with pytest.raises(anthropic.RateLimitError):
        await paid_calls.budgeted_message(client, "judge.batch", **PARAMS)
    release.assert_awaited_once_with("hold")
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_usage_retains_hold(guarded):
    client, response, _, persist, release = guarded
    response.usage = None
    with pytest.raises(paid_calls.BudgetUnavailable, match="usage missing"):
        await paid_calls.budgeted_message(client, "judge.batch", **PARAMS)
    release.assert_not_awaited()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_streamed_search_reserves_before_provider_and_settles_final_usage(guarded):
    client, response, reserve, persist, _ = guarded
    stream = SimpleNamespace(get_final_message=AsyncMock(return_value=response),
                             response=SimpleNamespace(headers={'request-id': 'req-stream-receipt'}))
    context = AsyncMock()
    context.__aenter__.return_value = stream
    def start(**params):
        reserve.assert_awaited_once()
        assert 'stream_response' not in params
        return context
    client.messages.stream = Mock(side_effect=start)
    result = await paid_calls.budgeted_message(client, 'compare.search_sources', stream_response=True, **PARAMS)
    assert result is response
    assert result._request_id == 'req-stream-receipt'
    persist.assert_awaited_once()
    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupted_stream_keeps_uncertain_cost_reserved(guarded):
    client, _, _, persist, release = guarded
    stream = SimpleNamespace(get_final_message=AsyncMock(side_effect=asyncio.CancelledError))
    context = AsyncMock()
    context.__aenter__.return_value = stream
    client.messages.stream = Mock(return_value=context)
    with pytest.raises(asyncio.CancelledError):
        await paid_calls.budgeted_message(client, 'compare.search_sources', stream_response=True, **PARAMS)
    release.assert_not_awaited()
    persist.assert_not_awaited()
