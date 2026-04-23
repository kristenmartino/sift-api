"""Background task that polls Anthropic's Message Batches API for completion,
then routes results to kind-specific handlers that update Postgres.

Runs indefinitely while the app is up. Poll interval is short (60s) relative
to Railway refresh cadence (600s), so completed batches surface quickly.
"""
from __future__ import annotations

import asyncio
import logging

from services.batch_client import poll_pending_batches
from services.context_generator import (
    BATCH_KIND as CONTEXT_BATCH_KIND,
    process_context_batch_results,
)
from services.entity_extractor import (
    BATCH_KIND as ENTITY_BATCH_KIND,
    process_entity_batch_results,
)

logger = logging.getLogger("sift-api.batch_poller")

POLL_INTERVAL_SECONDS = 60


# Kind → async handler(batch_id, results_list)
HANDLERS = {
    CONTEXT_BATCH_KIND: process_context_batch_results,
    ENTITY_BATCH_KIND: process_entity_batch_results,
}


async def run_batch_poller() -> None:
    """Poll loop. Survives individual iteration errors."""
    logger.info("Batch poller started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await poll_pending_batches(HANDLERS)
        except asyncio.CancelledError:
            logger.info("Batch poller cancelled")
            raise
        except Exception as e:
            logger.error("Batch poller iteration failed: %s", e)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
