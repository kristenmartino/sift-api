"""Bounded original descriptions from National Review's public RSS feed."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import feedparser
import httpx

from services.compare_coverage import _has_publisher_reporting
from services.compare_search import _article_identity, publisher_domains

logger = logging.getLogger('sift-api.compare')
PUBLIC_FEED = 'https://www.nationalreview.com/feed/'
MAX_FEED_BYTES = 1_000_000
FEED_TIMEOUT = 2
PUBLIC_DESCRIPTION_PREFIX = 'Public publisher description (not full article text).'


def _feed_article_excerpt(data: bytes, article_url: str, now: datetime) -> str | None:
    identity = _article_identity(article_url)
    for entry in feedparser.parse(data).entries[:50]:
        if _article_identity(entry.get('link', '')) != identity:
            continue
        published = entry.get('published_parsed')
        if not published:
            continue
        stamp = datetime(*published[:6], tzinfo=timezone.utc)
        if not now - timedelta(days=14) <= stamp <= now:
            continue
        title = entry.get('title', '')
        # NR's content:encoded can be only a photo caption, longer than the
        # actual editorial description. Never turn that caption into reporting.
        body = unescape(re.sub(r'<[^>]*>', ' ', entry.get('summary', '')))
        body = re.sub(r'\s+', ' ', body).strip()
        if not _has_publisher_reporting(body, title):
            continue
        return (f'{PUBLIC_DESCRIPTION_PREFIX}\nHeadline: {title}\n'
                f'Original: {entry["link"]}\nPublic RSS: {PUBLIC_FEED}\n'
                f'Published: {stamp.isoformat()}\nExcerpt:\n{body[:12000]}')
    return None


async def public_feed_excerpt(article_url: str, source: str) -> str | None:
    """Read the configured public feed only for an exact known NR article.

    No article-page requests, redirects, credentials or access-control workarounds.
    Failure/missing descriptions leave the existing single search fallback intact.
    """
    identity = _article_identity(article_url)
    if (publisher_domains(source) != ('nationalreview.com',) or not identity
            or not article_url.startswith('https://') or identity[0] != 'nationalreview.com'
            or not identity[1] or identity[2]):
        return None
    try:
        async with asyncio.timeout(FEED_TIMEOUT):
            async with httpx.AsyncClient(timeout=FEED_TIMEOUT, follow_redirects=False) as client:
                async with client.stream('GET', PUBLIC_FEED) as response:
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if response.status_code != 200 or not content_type.endswith(('/xml', '+xml')):
                        return None
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_FEED_BYTES:
                            return None
            return _feed_article_excerpt(bytes(data), article_url, datetime.now(timezone.utc))
    except Exception as exc:
        logger.info('compare public publisher feed unavailable (%s); using search', type(exc).__name__)
        return None
