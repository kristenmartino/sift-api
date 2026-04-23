from __future__ import annotations

import json
import logging

from app.db import get_pool
from app.models import RSSArticle

logger = logging.getLogger("sift-api.deduplicator")


async def deduplicate(articles: list[RSSArticle]) -> list[RSSArticle]:
    """Drop articles already in the DB by source_url OR content_hash.

    Content-hash match catches syndicated copies (AP → NPR / Yahoo / ABC) whose
    URL differs but body text is identical. Both checks happen in a single
    round-trip before any Claude call.
    """
    if not articles:
        return []

    pool = await get_pool()
    urls = [a.source_url for a in articles]
    hashes = [a.content_hash for a in articles if a.content_hash]

    # Single query: fetch any existing row whose URL or content_hash matches.
    rows = await pool.fetch(
        """
        SELECT source_url, content_hash
          FROM articles
         WHERE source_url = ANY($1::text[])
            OR content_hash = ANY($2::text[])
        """,
        urls,
        hashes,
    )
    existing_urls = {row["source_url"] for row in rows if row["source_url"]}
    existing_hashes = {row["content_hash"] for row in rows if row["content_hash"]}

    new_articles: list[RSSArticle] = []
    seen_hashes: set[str] = set()
    dropped_url = 0
    dropped_hash_db = 0
    dropped_hash_intra = 0

    for a in articles:
        if a.source_url in existing_urls:
            dropped_url += 1
            continue
        if a.content_hash and a.content_hash in existing_hashes:
            dropped_hash_db += 1
            continue
        # Intra-batch dedup: two feeds delivering the same story in one run.
        if a.content_hash and a.content_hash in seen_hashes:
            dropped_hash_intra += 1
            continue
        if a.content_hash:
            seen_hashes.add(a.content_hash)
        new_articles.append(a)

    skipped = dropped_url + dropped_hash_db + dropped_hash_intra
    if skipped > 0 or new_articles:
        logger.info(json.dumps({
            "event": "dedup_stats",
            "total": len(articles),
            "new": len(new_articles),
            "dropped_url": dropped_url,
            "dropped_hash_db": dropped_hash_db,
            "dropped_hash_intra": dropped_hash_intra,
        }))

    return new_articles
