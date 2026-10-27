"""Read original publisher excerpts for an exact, already indexed event."""
from __future__ import annotations

import logging
import re
from html import unescape

from app.db import get_pool

logger = logging.getLogger("sift-api.compare")

INDEXED_COVERAGE_SQL = """
WITH publisher AS (
    SELECT slug, name FROM outlet_profiles
    WHERE LOWER(name) = LOWER($2) OR LOWER(slug) = LOWER($2)
    UNION
    SELECT p.slug, p.name FROM outlet_profiles p
    JOIN source_name_aliases sa ON sa.outlet_slug = p.slug
    WHERE LOWER(sa.raw_source_name) = LOWER($2)
), names AS (
    SELECT LOWER(name) AS name FROM publisher
    UNION
    SELECT LOWER(sa.raw_source_name) FROM source_name_aliases sa
    JOIN publisher p ON p.slug = sa.outlet_slug
), event AS (
    SELECT id FROM stories WHERE LOWER(headline) = LOWER($1) AND synthesis_status = 'complete'
)
SELECT a.title, a.source_url, a.published_date, a.generation_raw_content
FROM articles a
WHERE LOWER(a.source_name) IN (SELECT name FROM names)
  AND (a.story_id IN (SELECT id FROM event) OR LOWER(a.title) = LOWER($1))
  AND a.published_date >= NOW() - INTERVAL '14 days'
  AND a.published_date <= NOW()
  AND NOT a.from_search AND NOT a.is_roundup
  AND a.generation_status = 'complete' AND a.summary IS NOT NULL
  AND a.source_url ~ '^https?://'
ORDER BY a.published_date DESC
LIMIT 1
"""


async def indexed_coverage(topic: str, source: str) -> dict[str, str | None] | None:
    """Require exact event/title identity; never treat a broad topic as an event.

    Only the stored publisher excerpt is evidence. The generated summary is a
    publication gate, never input to the comparison. Misses use live search.
    """
    try:
        pool = await get_pool()
        row = await pool.fetchrow(INDEXED_COVERAGE_SQL, topic.strip(), source.strip(), timeout=3)
        if not row:
            return None
        body = unescape(re.sub(r"<[^>]*>", " ", row["generation_raw_content"] or ""))
        body = re.sub(r"\s+", " ", body).strip()
        excerpt = None
        if len(body) >= 200:
            excerpt = (f"Stored publisher excerpt (may be incomplete).\nHeadline: {row['title']}\n"
                       f"Original: {row['source_url']}\nPublished: {row['published_date'].isoformat()}\n"
                       f"Excerpt:\n{body[:12000]}")
        return {"excerpt": excerpt, "title": row["title"], "url": row["source_url"]}
    except Exception:
        logger.warning("compare indexed coverage unavailable for %s; using live search", source)
        return None
