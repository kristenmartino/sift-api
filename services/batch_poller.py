"""Background task that polls Anthropic's Message Batches API for completion,
then routes results to kind-specific handlers that update Postgres.

Runs while the app is up, but only *polls* while something is in flight. The
poll interval stays short (60s) relative to the Railway refresh cadence
(1800s), so completed batches still surface quickly.

This task used to loop unconditionally every 60s, and poll_pending_batches
opened with a SELECT — so a deployment with no batches pending still issued
1,440 queries a day, each one landing inside Neon's 300s scale-to-zero window
and resetting it. The compute could therefore never suspend: measured
2026-08-14, pg_postmaster_start_time() reported 26 days of unbroken uptime,
~730 CU-hours/month against a 300 CU-hour allowance. Now the loop blocks on an
event that submit_batch sets, and issues no queries at all while idle.
"""
from __future__ import annotations

import asyncio
import logging

from services import batch_client
from services.batch_client import poll_pending_batches
from services.context_generator import (
    BATCH_KIND as CONTEXT_BATCH_KIND,
    process_context_batch_results,
)
from services.entity_extractor import (
    BATCH_KIND as ENTITY_BATCH_KIND,
    process_entity_batch_results,
)
from services.primer_generator import (
    BATCH_KIND as PRIMER_BATCH_KIND,
    process_primer_batch_results,
)

logger = logging.getLogger("sift-api.batch_poller")

POLL_INTERVAL_SECONDS = 60


# Kind → async handler(batch_id, results_list)
HANDLERS = {
    CONTEXT_BATCH_KIND: process_context_batch_results,
    ENTITY_BATCH_KIND: process_entity_batch_results,
    PRIMER_BATCH_KIND: process_primer_batch_results,
}


async def run_batch_poller() -> None:
    """Poll only while something is in flight. Survives iteration errors.

    One DB read at startup recovers anything left in flight across a restart;
    after that the loop blocks on batch_client's signal, which submit_batch
    sets. While idle this task issues zero queries and holds no connection.
    While busy the cadence is unchanged at 60s, and those ticks are Anthropic
    API calls only — Postgres is touched when a batch actually ends.
    """
    logger.info("Batch poller started (interval=%ds, event-driven)", POLL_INTERVAL_SECONDS)
    try:
        await batch_client.sync_pending_from_db()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("Batch recovery read failed: %s", e)

    while True:
        try:
            await batch_client.wait_for_pending()
            await poll_pending_batches(HANDLERS)
        except asyncio.CancelledError:
            logger.info("Batch poller cancelled")
            raise
        except Exception as e:
            logger.error("Batch poller iteration failed: %s", e)
        # Guard, not decoration: without it a poll that drained the last batch
        # would still sleep 60s before returning to wait_for_pending(). With
        # it, "idle ⇒ blocked, not sleeping" is exactly true — which is the
        # property the tests assert and the whole point of the change.
        if batch_client.has_pending():
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
