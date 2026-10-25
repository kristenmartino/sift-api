"""Durable summary retries on the existing pipeline cadence."""
from app.db import get_pool
from app.models import RSSArticle

RETRY_LIMIT = 50


async def pending_articles() -> list[RSSArticle]:
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT title, source_url, source_name, image_url, published_date,
               content_hash, generation_raw_content
          FROM articles
         WHERE generation_status = 'pending' AND from_search = false
           AND COALESCE(generation_raw_content, '') <> ''
         ORDER BY generation_attempted_at NULLS FIRST, created_at
         LIMIT $1
    """, RETRY_LIMIT)
    return [RSSArticle(title=r["title"], source_url=r["source_url"],
                       source_name=r["source_name"] or "", image_url=r["image_url"],
                       published_date=r["published_date"], content_hash=r["content_hash"],
                       raw_content=r["generation_raw_content"]) for r in rows]
