from __future__ import annotations

import asyncio
import logging

import voyageai

from app.config import settings

logger = logging.getLogger("sift-api.embedder")

MODEL = "voyage-3-lite"
BATCH_SIZE = 128  # Voyage AI max batch size


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of texts using Voyage AI voyage-3-lite.
    Returns list of 512-dim vectors in the same order as inputs.
    """
    if not texts:
        return []

    client = voyageai.Client(api_key=settings.voyage_api_key)
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        try:
            # voyageai.Client.embed is synchronous, run in thread pool
            result = await asyncio.to_thread(
                client.embed,
                batch,
                model=MODEL,
                input_type="document",
            )
            all_embeddings.extend(result.embeddings)
        except Exception as e:
            logger.error("Embedding failed for batch %d: %s", i // BATCH_SIZE, e)
            # Return zero vectors as fallback for this batch
            all_embeddings.extend([[0.0] * 512 for _ in batch])

    logger.info("Embedded %d texts (%d-dim vectors)", len(all_embeddings), 512)
    return all_embeddings
