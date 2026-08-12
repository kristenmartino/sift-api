"""submit_batch refuses a non-Anthropic model rather than degrading quietly.

This endpoint is Anthropic's. Posting another provider's model id to it fails,
`submit_batch` returns None, and the caller's contract — "the articles simply
go without context for now" (services/context_generator.submit_context_batch) —
swallows that as a routine degrade. The columns stay NULL, the run reports
success, and nothing distinguishes it from a normal miss.

`model_registry.resolve` already refuses to move these three stages onto a
model without a batch API. This is the second door: a request body assembled
some other way — a script, a future caller, a hand-built retry — must not reach
the wire and fail silently.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import batch_client


def _request(model: str) -> dict:
    return {
        "custom_id": "ctx-1",
        "params": {
            "model": model,
            "max_tokens": 850,
            "messages": [{"role": "user", "content": "hi"}],
        },
    }


@pytest.mark.asyncio
async def test_a_foreign_model_is_refused_before_the_wire(monkeypatch, caplog):
    client = AsyncMock()
    monkeypatch.setattr(batch_client, "_client", lambda: client)

    with caplog.at_level("ERROR", logger="sift-api.batch_client"):
        result = await batch_client.submit_batch(
            "context", [_request("some-open-weight-model")]
        )

    assert result is None
    client.messages.batches.create.assert_not_called()
    assert caplog.records, "a refusal that logs nothing is a silent degrade"
    assert "not an Anthropic model" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_one_foreign_request_refuses_the_whole_batch(monkeypatch, caplog):
    """All-or-nothing: a partial submit would land some articles and silently
    drop others, which is the harder failure to notice of the two."""
    client = AsyncMock()
    monkeypatch.setattr(batch_client, "_client", lambda: client)

    with caplog.at_level("ERROR", logger="sift-api.batch_client"):
        result = await batch_client.submit_batch(
            "context",
            [_request("claude-haiku-4-5-20251001"), _request("some-other-model")],
        )

    assert result is None
    client.messages.batches.create.assert_not_called()


@pytest.mark.asyncio
async def test_both_the_dated_snapshot_and_the_alias_are_accepted(monkeypatch):
    """Both forms are real: the batch paths logged the alias and the realtime
    paths the dated snapshot until 2026-08-12, and old request bodies may carry
    either. Refusing the alias would break the stages this protects."""
    for model in ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-sonnet-4-6"):
        client = AsyncMock()
        client.messages.batches.create = AsyncMock(
            return_value=type("B", (), {"id": "batch_123"})()
        )
        monkeypatch.setattr(batch_client, "_client", lambda c=client: c)
        monkeypatch.setattr(
            batch_client, "get_pool", AsyncMock(return_value=AsyncMock())
        )

        result = await batch_client.submit_batch("context", [_request(model)])
        assert result == "batch_123", model


@pytest.mark.asyncio
async def test_an_empty_batch_is_still_a_noop(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(batch_client, "_client", lambda: client)
    assert await batch_client.submit_batch("context", []) is None
    client.messages.batches.create.assert_not_called()
