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

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# In-flight registry
#
# poll_pending_batches used to answer "which batches are outstanding" with a
# SELECT, every 60 seconds, forever (services/batch_poller.py). That query
# returns nothing on the overwhelming majority of runs, and on a deployment
# with no traffic its only effect was to reset Neon's scale-to-zero timer
# 1,440 times a day. The compute could therefore never suspend: measured
# 2026-08-14, pg_postmaster_start_time() showed 26 days of unbroken uptime,
# billing ~730 CU-hours/month against a 300 CU-hour allowance.
#
# The set is knowable without asking Postgres. Every submitter runs in this
# process — all three submit_*_batch calls are made from
# workflows/pipeline_workflow.py, inside the same event loop as the poller
# (app/main.py) — so submit_batch can simply record what it created.
# Postgres is consulted once at startup, to adopt anything a previous process
# left in flight, and thereafter only to WRITE, never to discover.
# ---------------------------------------------------------------------------

_pending: dict[str, str] = {}                # batch_id -> kind
_submitted_at: dict[str, datetime] = {}      # batch_id -> when we first saw it
_signal: asyncio.Event | None = None
_signal_loop: asyncio.AbstractEventLoop | None = None

# Anthropic expires batches at 24h. Stop asking after 26 and record the
# verdict, so a batch the API loses cannot keep the poller — and therefore the
# Neon compute — awake forever. The old code had no such bound: a batch whose
# results download kept failing was retried every 60s until the process died.
BATCH_GIVEUP_HOURS = 26


def _get_signal() -> asyncio.Event:
    """The poller's wakeup, created lazily against the running loop.

    A module-level asyncio.Event binds to the first loop that awaits it, and
    pytest-asyncio gives every test its own loop (asyncio_default_fixture_loop_scope
    = "function", pyproject.toml), so an Event constructed at import time
    raises "bound to a different event loop" on the second test that touches
    it. Re-creating on loop change leaves the production path — one loop, for
    the life of the process — exactly as it would be.
    """
    global _signal, _signal_loop
    loop = asyncio.get_running_loop()
    if _signal is None or _signal_loop is not loop:
        _signal = asyncio.Event()
        _signal_loop = loop
        if _pending:
            _signal.set()
    return _signal


def register_pending(batch_id: str, kind: str) -> None:
    """Adopt a batch into the in-flight set and wake the poller. Idempotent."""
    if batch_id not in _pending:
        _submitted_at[batch_id] = datetime.now(timezone.utc)
    _pending[batch_id] = kind
    _get_signal().set()


def _forget(batch_id: str) -> None:
    """Drop a settled batch. Clears the signal when nothing is left in flight."""
    _pending.pop(batch_id, None)
    _submitted_at.pop(batch_id, None)
    if not _pending:
        _get_signal().clear()


def has_pending() -> bool:
    return bool(_pending)


async def wait_for_pending() -> None:
    """Block until at least one batch is in flight. Issues no queries."""
    await _get_signal().wait()


async def sync_pending_from_db() -> int:
    """Merge the outstanding set from Postgres into the in-memory one.

    Called once when the poller starts (recovering batches left in flight
    across a restart or deploy) and once at the end of each pipeline run —
    which is already an awake window, so it costs no extra wake, and is the
    safety net for a batch submitted by some future out-of-process writer.

    Deliberately NOT on a timer. A timer here is the defect this whole
    mechanism removes.

    Returns the number of batches newly adopted.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT batch_id, kind FROM api_batches WHERE status = 'processing' "
        "ORDER BY submitted_at"
    )
    adopted = 0
    for row in rows:
        if row["batch_id"] not in _pending:
            adopted += 1
        register_pending(row["batch_id"], row["kind"])
    if adopted:
        logger.info("Adopted %d in-flight batch(es) from api_batches", adopted)
    return adopted


def _reset_for_tests() -> None:
    """Clear module state between tests. Wired into an autouse conftest fixture."""
    global _signal, _signal_loop
    _pending.clear()
    _submitted_at.clear()
    _signal = None
    _signal_loop = None


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

    # Signal the poller. Deliberately outside the try above: if the api_batches
    # INSERT failed we still want this batch's results applied during this
    # process lifetime. The row is the crash-recovery record, not the source of
    # truth for a live process — and _mark_status on completion then becomes a
    # no-op UPDATE, which is harmless.
    register_pending(batch.id, kind)

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
    """Check every in-flight batch. For each that has ended, stream the JSONL
    results and invoke handlers[kind](batch_id, results).

    Reads the in-flight set from memory (see register_pending above). Postgres
    is touched only when a batch has actually ended — never to find out whether
    one has. A poll that ends nothing issues no queries at all, which is what
    lets the Neon compute suspend between pipeline runs.

    Results shape per line:
        {"custom_id": "...", "result": {"type": "succeeded"|"errored", "message": {...}}}
    """
    if not _pending:
        return

    client = _client()
    # Acquired on first write. Keeping the acquisition next to the writes makes
    # the "nothing ended ⇒ nothing touched" property visible rather than implied.
    pool = None

    now = datetime.now(timezone.utc)
    for batch_id, kind in list(_pending.items()):
        submitted = _submitted_at.get(batch_id, now)
        if now - submitted > timedelta(hours=BATCH_GIVEUP_HOURS):
            logger.error(
                "Batch %s (kind=%s) still not ended after %dh — giving up",
                batch_id, kind, BATCH_GIVEUP_HOURS,
            )
            pool = pool or await get_pool()
            await _mark_status(pool, batch_id, "expired")
            _forget(batch_id)
            continue

        try:
            batch = await client.messages.batches.retrieve(batch_id)
        except Exception as e:
            logger.error("batches.retrieve(%s) failed: %s", batch_id, e)
            continue

        if batch.processing_status != "ended":
            continue

        pool = pool or await get_pool()

        # Download JSONL results.
        results_url = getattr(batch, "results_url", None)
        if not results_url:
            logger.error("Batch %s ended but has no results_url", batch_id)
            await _mark_status(pool, batch_id, "errored")
            _forget(batch_id)
            continue

        try:
            parsed = await _fetch_results_jsonl(results_url)
        except Exception as e:
            # Left pending on purpose, and retried next tick — unchanged
            # behaviour. BATCH_GIVEUP_HOURS now bounds how long that can go on.
            logger.error("Failed to fetch results for %s: %s", batch_id, e)
            continue

        handler = handlers.get(kind)
        if handler is None:
            logger.warning("No handler registered for batch kind=%s (batch=%s)", kind, batch_id)
            await _mark_status(pool, batch_id, "succeeded")
            _forget(batch_id)
            continue

        try:
            await handler(batch_id, parsed)
        except Exception as e:
            logger.error("Handler for kind=%s batch=%s failed: %s", kind, batch_id, e)
            await _mark_status(pool, batch_id, "errored")
            _forget(batch_id)
            continue

        await _mark_status(pool, batch_id, "succeeded")
        _forget(batch_id)
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
