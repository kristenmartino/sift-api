"""Read original publisher excerpts for an exact, already indexed event."""
from __future__ import annotations

import asyncio
import logging
import re
from html import unescape

from app.db import get_pool

logger = logging.getLogger("sift-api.compare")
# A measured first lookup after deployment took 3.74s; the old 3s query
# deadline discarded indexed evidence when the database resumed. Bound the
# entire lookup (including pool acquisition), leaving time for search fallback
# and extraction within Compare's existing 50s request deadline.
INDEXED_LOOKUP_TIMEOUT = 8

# RSS descriptions can contain usable reporting in fewer than 200 characters.
# This is a minimum evidence check, not a completeness test: extraction must
# continue to treat accepted text as an incomplete publisher excerpt.
_ACCESS_PROMPT = re.compile(
    r"^(?:subscribe\b|sign (?:in|up)\b|log in\b|read more\b|continue reading\b)",
    re.IGNORECASE,
)


def _has_publisher_reporting(body: str, title: str, *, minimum_words: int = 20) -> bool:
    if not body or _ACCESS_PROMPT.match(body):
        return False
    # A long headline repeated as the description is still only metadata.
    def normalized(text: str) -> str:
        return " ".join(re.findall(r"\w+", text.casefold()))

    if normalized(body) == normalized(title):
        return False
    return len(body.split()) >= minimum_words


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
    SELECT id FROM stories
    WHERE (LOWER(headline) = LOWER($1) OR headline_aliases @> ARRAY[LOWER($1)])
      AND synthesis_status = 'complete'
), matched_articles AS MATERIALIZED (
    SELECT id FROM articles WHERE story_id IN (SELECT id FROM event)
    UNION
    SELECT id FROM articles WHERE LOWER(title) = LOWER($1)
)
SELECT a.title, a.source_url, a.published_date, a.generation_raw_content,
       a.generation_status, a.summary IS NOT NULL AS reviewed
FROM articles a
JOIN matched_articles m ON m.id = a.id
WHERE LOWER(a.source_name) IN (SELECT name FROM names)
  AND a.published_date >= NOW() - INTERVAL '14 days'
  AND a.published_date <= NOW()
  AND NOT a.from_search AND NOT a.is_roundup
  AND a.source_url ~ '^https?://'
ORDER BY a.published_date DESC
LIMIT 8
"""

# Discovery only: use the existing article full-text index to locate original
# publisher titles. Never return generated summaries or credit a loose match
# as coverage. The budgeted publisher search still verifies relevance/evidence.
PUBLISHER_HINTS_SQL = """
SELECT title, source_url, published_date, generation_status,
       summary IS NOT NULL AS reviewed, generation_raw_content,
       ts_rank_cd(to_tsvector('english', COALESCE(title, '')),
                  websearch_to_tsquery('english', $1)) AS title_rank
FROM articles
WHERE LOWER(source_name) = LOWER($2)
  AND published_date >= NOW() - INTERVAL '14 days' AND published_date <= NOW()
  AND NOT from_search AND NOT is_roundup AND source_url ~ '^https://'
  AND to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(summary, ''))
      @@ websearch_to_tsquery('english', $1)
  AND ts_rank_cd(to_tsvector('english', COALESCE(title, '')),
                 websearch_to_tsquery('english', $1)) > 0
ORDER BY title_rank DESC, published_date DESC
LIMIT 3
"""


async def publisher_article_hints(topic: str, source: str = 'National Review') -> list[dict[str, str]]:
    words = list(dict.fromkeys(re.findall(r"[^\W\d_][\w'-]{2,59}", topic[:500].casefold())))[:12]
    if not words:
        return []
    try:
        async with asyncio.timeout(INDEXED_LOOKUP_TIMEOUT):
            pool = await get_pool()
            rows = await pool.fetch(PUBLISHER_HINTS_SQL, ' OR '.join(words), source.strip(), timeout=INDEXED_LOOKUP_TIMEOUT)
        best = float(rows[0]['title_rank']) if rows else 0
        hints = []
        for row in rows:
            if float(row['title_rank']) < best / 2:
                continue
            hint = {'title': row['title'], 'url': row['source_url']}
            body = unescape(re.sub(r'<[^>]*>', ' ', row['generation_raw_content'] or ''))
            body = re.sub(r'\s+', ' ', body).strip()
            if row['generation_status'] == 'complete' and row['reviewed'] and _has_publisher_reporting(body, row['title']):
                hint['excerpt'] = (f"Stored publisher excerpt (may be incomplete).\nHeadline: {row['title']}\n"
                                   f"Original: {row['source_url']}\nPublished: {row['published_date'].isoformat()}\n"
                                   f"Excerpt:\n{body[:12000]}")
            hints.append(hint)
        return hints
    except Exception as exc:
        logger.warning('compare publisher discovery unavailable (%s); using topic search', type(exc).__name__)
        return []


async def indexed_coverage(topic: str, source: str) -> dict[str, str | None] | None:
    """Require exact event/title identity; never treat a broad topic as an event.

    Only the stored publisher excerpt is evidence. The generated summary is a
    publication gate, never input to the comparison. Misses use live search.
    """
    try:
        async with asyncio.timeout(INDEXED_LOOKUP_TIMEOUT):
            pool = await get_pool()
            rows = await pool.fetch(
                INDEXED_COVERAGE_SQL, topic.strip(), source.strip(), timeout=INDEXED_LOOKUP_TIMEOUT,
            )
        if not rows:
            return None
        for row in rows:
            if row['generation_status'] != 'complete' or not row['reviewed']:
                continue
            body = unescape(re.sub(r"<[^>]*>", " ", row["generation_raw_content"] or ""))
            body = re.sub(r"\s+", " ", body).strip()
            if _has_publisher_reporting(body, row["title"]):
                excerpt = (f"Stored publisher excerpt (may be incomplete).\nHeadline: {row['title']}\n"
                           f"Original: {row['source_url']}\nPublished: {row['published_date'].isoformat()}\n"
                           f"Excerpt:\n{body[:12000]}")
                return {"excerpt": excerpt, "title": row["title"], "url": row["source_url"]}
        # Publisher URLs remain useful retrieval hints even when Sift's own
        # generated copy is pending/held or the RSS text was too thin to review.
        return {"excerpt": None, "title": rows[0]["title"], "url": rows[0]["source_url"]}
    except Exception as exc:
        logger.warning("compare indexed coverage unavailable for %s (%s); using live search",
                       source, type(exc).__name__)
        return None
