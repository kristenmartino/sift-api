"""Backfill embeddings for articles with NULL embedding column.

Uses Voyage AI REST API directly with small batches and rate limiting
to stay within free tier limits (3 RPM, 10K TPM).
"""

import asyncio
import logging
import time

import httpx

from app.config import settings
from app.db import init_pool, get_pool, close_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50  # Moderate batch size for standard rate limits
DELAY_BETWEEN_BATCHES = 1  # seconds — standard rate limits with payment method


async def embed_batch(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via Voyage AI REST API."""
    resp = await client.post(
        "https://api.voyageai.com/v1/embeddings",
        json={
            "input": texts,
            "model": "voyage-3-lite",
            "input_type": "document",
        },
        headers={
            "Authorization": f"Bearer {settings.voyage_api_key}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return [d["embedding"] for d in data["data"]]


async def main():
    await init_pool()
    pool = await get_pool()

    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM articles WHERE embedding IS NULL AND summary IS NOT NULL"
    )
    total = row["cnt"]
    logger.info("Found %d articles needing embeddings", total)

    if total == 0:
        logger.info("Nothing to do")
        await close_pool()
        return

    embedded = 0

    async with httpx.AsyncClient() as client:
        while True:
            # Always re-query to avoid offset drift after updates
            rows = await pool.fetch(
                """SELECT id, title, summary
                   FROM articles
                   WHERE embedding IS NULL AND summary IS NOT NULL
                   ORDER BY id
                   LIMIT $1""",
                BATCH_SIZE,
            )

            if not rows:
                break

            texts = [f"{r['title']}. {r['summary']}" for r in rows]
            logger.info("Embedding batch of %d articles...", len(rows))

            try:
                vectors = await embed_batch(client, texts)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("Rate limited, waiting 60s...")
                    await asyncio.sleep(60)
                    continue
                raise

            for r, vec in zip(rows, vectors):
                embedding_str = "[" + ",".join(str(x) for x in vec) + "]"
                await pool.execute(
                    "UPDATE articles SET embedding = $1::vector, updated_at = NOW() WHERE id = $2",
                    embedding_str, r["id"],
                )

            embedded += len(rows)
            remaining = total - embedded
            logger.info("Embedded %d / %d articles (%d remaining)", embedded, total, remaining)

            if remaining > 0:
                logger.info("Waiting %ds for rate limit...", DELAY_BETWEEN_BATCHES)
                await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    logger.info("Done. Backfilled %d embeddings.", embedded)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
