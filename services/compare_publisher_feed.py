"""Bounded original reporting from verified public publisher RSS feeds."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import feedparser
import httpx

from services.compare_coverage import _has_publisher_reporting
from services.compare_search import _article_identity, publisher_search_domains

logger = logging.getLogger('sift-api.compare')
PUBLIC_FEED = 'https://www.nationalreview.com/feed/'
AXIOS_FEED = 'https://api.axios.com/feed/'
_FEEDS = {'nationalreview.com': PUBLIC_FEED, 'axios.com': AXIOS_FEED}
MAX_FEED_BYTES = 2_000_000
FEED_TIMEOUT = 2
PUBLIC_DESCRIPTION_PREFIX = 'Public publisher description (not full article text).'


def complete_description(body: str) -> bool:
    """Require a finished statement rather than a cut-off preview or CTA."""
    return (body.rstrip(" \"'”’").endswith(('.', '!', '?'))
            and not re.search(r'(?:\.\s*){3}|…', body)
            and not re.match(r'^(?:read\b|click\b|watch\b|find out\b|learn more\b|'
                             r'already have an account\b|unlock\b|get full access\b)', body, re.I))


def _feed_article_excerpt(data: bytes, article_url: str, now: datetime, *, feed_url: str = PUBLIC_FEED) -> str | None:
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
        title = unescape(re.sub(r'<[^>]*>', ' ', entry.get('title', '')))
        title = re.sub(r'\s+', ' ', title).strip()
        # NR's content:encoded can be only a photo caption, longer than the
        # actual editorial description. Never turn that caption into reporting.
        summary = entry.get('summary', '')
        body = unescape(re.sub(r'<[^>]*>', ' ', summary))
        body = re.sub(r'\s+', ' ', body).strip()
        if feed_url == AXIOS_FEED:
            # Axios publishes article paragraphs followed by an unpunctuated
            # document/image caption. Do not discard the reporting or turn
            # that caption into a statement. Keep only whole public blocks.
            paragraphs = []
            for block in re.findall(r'<(?:p|li)\b[^>]*>(.*?)</(?:p|li)>', summary, re.I | re.S):
                text = ' '.join(unescape(re.sub(r'<[^>]*>', ' ', block)).split())
                if _has_publisher_reporting(text, title, minimum_words=6) and complete_description(text):
                    if sum(map(len, paragraphs)) + len(text) > 12000:
                        break
                    paragraphs.append(text)
            body = '\n\n'.join(paragraphs)

        if not _has_publisher_reporting(body, title, minimum_words=6) or not complete_description(body):
            continue
        return (f'{PUBLIC_DESCRIPTION_PREFIX}\nHeadline: {title}\n'
                f'Original: {entry["link"]}\nPublic RSS: {feed_url}\n'
                f'Published: {stamp.isoformat()}\nExcerpt:\n{body[:12000]}')
    return None


async def public_feed_excerpt(article_url: str, source: str) -> str | None:
    return (await public_feed_excerpts([article_url], source)).get(article_url)


async def public_feed_excerpts(article_urls: list[str], source: str) -> dict[str, str]:
    """Read the fixed public feed once for up to three known publisher URLs.

    Discovery candidates still require the caller to verify topic relevance.
    No article-page requests, redirects, credentials or access-control workarounds.
    Failure/missing descriptions leave the existing single search fallback intact.
    """
    domains = publisher_search_domains(source)
    if len(domains) != 1 or domains[0] not in _FEEDS:
        return {}
    host = domains[0]
    feed_url = _FEEDS[host]
    valid = []
    for article_url in dict.fromkeys(article_urls[:3]):
        identity = _article_identity(article_url)
        if (identity and article_url.startswith('https://') and identity[0] == host
                and identity[1] and not identity[2]):
            valid.append(article_url)
    if not valid:
        return {}
    try:
        async with asyncio.timeout(FEED_TIMEOUT):
            async with httpx.AsyncClient(timeout=FEED_TIMEOUT, follow_redirects=False) as client:
                async with client.stream('GET', feed_url) as response:
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if response.status_code != 200 or not content_type.endswith(('/xml', '+xml')):
                        return {}
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_FEED_BYTES:
                            return {}
            now = datetime.now(timezone.utc)
            excerpts = {url: _feed_article_excerpt(bytes(data), url, now, feed_url=feed_url) for url in valid}
            return {url: text for url, text in excerpts.items() if text}
    except Exception as exc:
        logger.info('compare public publisher feed unavailable (%s); using search', type(exc).__name__)
        return {}
