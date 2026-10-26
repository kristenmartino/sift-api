from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services import batch_client
from services.usage_tracker import batch_already_metered, log_batch_usage


@pytest.mark.asyncio
async def test_batch_admission_is_before_provider_submission(monkeypatch):
    monkeypatch.setattr(batch_client.settings, "ai_cost_guard_enabled", True)
    monkeypatch.setattr(batch_client, "reserve_budget", AsyncMock(return_value=None))
    factory = Mock()
    monkeypatch.setattr(batch_client, "_client", factory)
    assert (
        await batch_client.submit_batch(
            "context",
            [
                {
                    "custom_id": "test",
                    "params": {
                        "model": "claude-haiku-4-5",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "test"}],
                    },
                }
            ],
        )
        is None
    )
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_batch_is_settled_before_handler_and_not_recorded_twice(monkeypatch):
    from services import usage_tracker

    monkeypatch.setattr(batch_client.settings, "ai_cost_guard_enabled", True)
    pool = AsyncMock()
    pool.fetchval.return_value = "durable-batch-hold"
    monkeypatch.setattr(batch_client, "get_pool", AsyncMock(return_value=pool))
    client = SimpleNamespace(
        messages=SimpleNamespace(
            batches=SimpleNamespace(
                retrieve=AsyncMock(
                    return_value=SimpleNamespace(
                        processing_status="ended", results_url="https://test.invalid/results"
                    )
                )
            )
        )
    )
    monkeypatch.setattr(batch_client, "_client", lambda: client)
    results = [
        {
            "result": {
                "type": "succeeded",
                "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 100, "output_tokens": 20}},
            }
        }
    ]
    monkeypatch.setattr(batch_client, "_fetch_results_jsonl", AsyncMock(return_value=results))
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(batch_client, "persist_payload", persist)
    legacy_record = Mock()
    monkeypatch.setattr(usage_tracker, "_record_to_ledger", legacy_record)

    async def handler(*_):
        persist.assert_awaited_once()
        assert batch_already_metered.get() is True
        log_batch_usage("context_generator.batch", results)

    batch_client.register_pending("batch-test", "context")
    await batch_client.poll_pending_batches({"context": handler})
    legacy_record.assert_not_called()
    assert not batch_client.has_pending()
    assert batch_already_metered.get() is False


@pytest.mark.asyncio
async def test_batch_settlement_failure_keeps_results_pending(monkeypatch):
    monkeypatch.setattr(batch_client.settings, "ai_cost_guard_enabled", True)
    pool = AsyncMock()
    pool.fetchval.return_value = "durable-batch-hold"
    monkeypatch.setattr(batch_client, "get_pool", AsyncMock(return_value=pool))
    client = SimpleNamespace(
        messages=SimpleNamespace(
            batches=SimpleNamespace(
                retrieve=AsyncMock(
                    return_value=SimpleNamespace(
                        processing_status="ended", results_url="https://test.invalid/results"
                    )
                )
            )
        )
    )
    monkeypatch.setattr(batch_client, "_client", lambda: client)
    monkeypatch.setattr(
        batch_client, "_fetch_results_jsonl", AsyncMock(return_value=[{"result": {"type": "errored"}}])
    )
    monkeypatch.setattr(batch_client, "persist_payload", AsyncMock(return_value=False))
    handler = AsyncMock()
    batch_client.register_pending("batch-test", "context")
    await batch_client.poll_pending_batches({"context": handler})
    assert batch_client.has_pending()
    handler.assert_not_awaited()
