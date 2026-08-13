"""Thin wrapper around Anthropic's Message Batches API.

Batches get a flat 50% discount on both input and output tokens vs the
realtime Messages API, at the cost of up to 24h SLA (typically minutes).

Workflow:
  1. submit_batch(kind, requests) → returns batch_id, persists row in
     api_batches with status='processing'.
  2. Poller periodically calls poll_pending_batches() which retrieves
     status. When 'ended', it streams the JSONL results and passes them
     to a kind-specific handler.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

import anthropic
import httpx

from app.config import settings
from app.db import get_pool

logger = logging.getLogger("sift-api.batch_client")

# No MODEL constant here. This module submits whatever `requests` carries, and
# every caller builds its own body from services/model_registry.resolve(). The
# constant that used to sit here was never read by anything — a second, silent
# answer to "which model do batches use" that could drift from the real one.


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def submit_batch(kind: str, requests: list[dict], metadata: dict | None = None) -> str | None:
    """Submit a batch of message requests and record it in api_batches.

    Each request must have {'custom_id': str, 'params': {model, max_tokens, messages, ...}}.
    Returns the Anthropic batch_id, or None if submission failed.
    """
    if not requests:
        return None

    # This endpoint is Anthropic's. Posting another provider's model id to it
    # fails, submit_batch returns None, and the caller's contract — "the
    # articles simply go without context for now" — swallows it as a normal
    # degrade. That is a silent quality regression indistinguishable from a
    # routine miss, so refuse loudly at the door instead.
    #
    # model_registry.resolve already refuses to move these stages onto a model
    # without a batch API; this catches a request body assembled some other way.
    # Matched through spec_for_wire_model so the undated alias is accepted as
    # well as the dated snapshot — both are real forms, and refusing the alias
    # would break the very stages this protects.
    from services.model_registry import spec_for_wire_model

    def _is_foreign(model: str) -> bool:
        spec = spec_for_wire_model(model)
        return spec is None or spec.provider != "anthropic"

    foreign = sorted({
        m for r in requests
        if (m := (r.get("params") or {}).get("model")) and _is_foreign(m)
    })
    if foreign:
        logger.error(
            "submit_batch(%s) refused: %s is not an Anthropic model, and this "
            "is the Anthropic Message Batches endpoint. Nothing was submitted.",
            kind,
            ", ".join(foreign),
        )
        return None

    client = _client()
    try:
        batch = await client.messages.batches.create(requests=requests)
    except Exception as e:
        logger.error("submit_batch(%s) failed: %s", kind, e)
        return None

    pool = await get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO api_batches (batch_id, kind, status, metadata)
            VALUES ($1, $2, 'processing', $3::jsonb)
            ON CONFLICT (batch_id) DO NOTHING
            """,
            batch.id,
            kind,
            json.dumps(metadata or {}),
        )
    except Exception as e:
        logger.error("Failed to record batch %s in api_batches: %s", batch.id, e)

    logger.info(json.dumps({
        "event": "batch_submitted",
        "kind": kind,
        "batch_id": batch.id,
        "requests": len(requests),
    }))
    return batch.id


async def poll_pending_batches(
    handlers: dict[str, Callable[[str, list[dict]], Awaitable[None]]],
) -> None:
    """Poll every row where status='processing'. For each that has ended,
    stream the JSONL results and invoke handlers[kind](batch_id, results).

    Results shape per line:
        {"custom_id": "...", "result": {"type": "succeeded"|"errored", "message": {...}}}
    """
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT batch_id, kind FROM api_batches WHERE status = 'processing' ORDER BY submitted_at"
    )
    if not rows:
        return

    client = _client()
    for row in rows:
        batch_id = row["batch_id"]
        kind = row["kind"]
        try:
            batch = await client.messages.batches.retrieve(batch_id)
        except Exception as e:
            logger.error("batches.retrieve(%s) failed: %s", batch_id, e)
            continue

        if batch.processing_status != "ended":
            continue

        # Download JSONL results.
        results_url = getattr(batch, "results_url", None)
        if not results_url:
            logger.error("Batch %s ended but has no results_url", batch_id)
            await _mark_status(pool, batch_id, "errored")
            continue

        try:
            parsed = await _fetch_results_jsonl(results_url)
        except Exception as e:
            logger.error("Failed to fetch results for %s: %s", batch_id, e)
            continue

        handler = handlers.get(kind)
        if handler is None:
            logger.warning("No handler registered for batch kind=%s (batch=%s)", kind, batch_id)
            await _mark_status(pool, batch_id, "succeeded")
            continue

        try:
            await handler(batch_id, parsed)
        except Exception as e:
            logger.error("Handler for kind=%s batch=%s failed: %s", kind, batch_id, e)
            await _mark_status(pool, batch_id, "errored")
            continue

        await _mark_status(pool, batch_id, "succeeded")
        logger.info(json.dumps({
            "event": "batch_completed",
            "kind": kind,
            "batch_id": batch_id,
            "results": len(parsed),
        }))


async def _fetch_results_jsonl(url: str) -> list[dict]:
    """Download the batch results JSONL. Auth header required per Anthropic docs."""
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.get(url, headers=headers)
        resp.raise_for_status()
        lines = resp.text.splitlines()
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line")
    return out


async def _mark_status(pool, batch_id: str, status: str) -> None:
    try:
        await pool.execute(
            "UPDATE api_batches SET status = $1, completed_at = NOW() WHERE batch_id = $2",
            status, batch_id,
        )
    except Exception as e:
        logger.error("Failed to mark batch %s status=%s: %s", batch_id, status, e)
