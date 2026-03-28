from __future__ import annotations

import logging

from app.db import get_pool
from app.models import RSSArticle

logger = logging.getLogger("sift-api.deduplicator")


async def deduplicate(articles: list[RSSArticle]) -> list[RSSArticle]:
    """Filter out articles whose source_url already exists in the database."""
    if not articles:
        return []

    pool = await get_pool()
    urls = [a.source_url for a in articles]

    rows = await pool.fetch(
        "SELECT source_url FROM articles WHERE source_url = ANY($1::text[])",
        urls,
    )
    existing_urls = {row["source_url"] for row in rows}

    new_articles = [a for a in articles if a.source_url not in existing_urls]
    skipped = len(articles) - len(new_articles)

    if skipped > 0:
        logger.info("Deduplicated: %d new, %d skipped (already in DB)", len(new_articles), skipped)

    return new_articles
