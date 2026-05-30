from __future__ import annotations

import asyncio
import logging

import voyageai

from app.config import settings

logger = logging.getLogger("sift-api.embedder")

MODEL = "voyage-3-lite"
BATCH_SIZE = 128  # Voyage AI max batch size


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """
    Embed a list of texts using Voyage AI voyage-3-lite.

    Returns a list aligned 1:1 with ``texts``: a 512-dim vector per text, or
    ``None`` for any text whose batch failed to embed. Callers persist ``None``
    as a NULL embedding (never a zero vector), so a failed batch stays out of
    vector search and can be re-embedded later instead of polluting similarity
    results with ``[0.0] * 512``.
    """
    if not texts:
        return []

    client = voyageai.Client(api_key=settings.voyage_api_key)
    all_embeddings: list[list[float] | None] = []

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
            logger.error(
                "Embedding failed for batch %d (%d texts); emitting NULL "
                "embeddings (skipped, re-embeddable later): %s",
                i // BATCH_SIZE,
                len(batch),
                e,
            )
            # Emit None per item (NULL embedding) — never a zero vector. Length
            # is preserved so the caller's article/vector alignment stays stable.
            all_embeddings.extend([None] * len(batch))

    embedded = sum(1 for v in all_embeddings if v is not None)
    skipped = len(all_embeddings) - embedded
    logger.info(
        "Embedded %d/%d texts (%d-dim); %d skipped (NULL embedding)",
        embedded,
        len(all_embeddings),
        512,
        skipped,
    )
    return all_embeddings
