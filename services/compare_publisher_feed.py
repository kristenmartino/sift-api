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
# Fixed feeds exercised with ordinary public HTTP; CNBC's rejected feed is
# intentionally excluded. FT's configured home feed redirects to this URL.
_FEEDS = {
    'nationalreview.com': PUBLIC_FEED, 'axios.com': AXIOS_FEED,
    'ft.com': 'https://www.ft.com/rss/home/international',
    'thehill.com': 'https://thehill.com/news/feed/',
    'theverge.com': 'https://www.theverge.com/rss/index.xml',
    'arstechnica.com': 'https://feeds.arstechnica.com/arstechnica/index',
}
# Public topic feed verified against FT's exact Iran memo. Choose it from
# article/topic words only; callers cannot supply a URL.
_FT_TOPIC_FEEDS = {r'\biran(?:ian)?\b': 'https://www.ft.com/iran?format=rss'}
MAX_FEED_BYTES = 2_000_000
FEED_TIMEOUT = 2
PUBLIC_DESCRIPTION_PREFIX = 'Public publisher description (not full article text).'


def supports_public_feed(source: str) -> bool:
    domains = publisher_search_domains(source)
    return len(domains) == 1 and domains[0] in _FEEDS


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
        if feed_url not in {PUBLIC_FEED, AXIOS_FEED}:
            # Some public Atom/RSS feeds put a truncated teaser in summary
            # and complete paragraphs in content. Only the feed's own content
            # is eligible; the article page and any gated schema are untouched.
            public_content = next((item.get('value', '') for item in entry.get('content', [])
                                   if item.get('type', '') in {'text/html', 'application/xhtml+xml'}), '')
            if re.search(r'<(?:p|li)\b', public_content, re.I):
                summary = public_content
        if feed_url == AXIOS_FEED or (feed_url != PUBLIC_FEED and re.search(r'<(?:p|li)\b', summary, re.I)):
            # Keep whole public blocks, excluding trailing captions, teasers
            # and incomplete paragraphs. NR retains its summary-only rule.
            paragraphs = []
            for block in re.findall(r'<(?:p|li)\b[^>]*>(.*?)</(?:p|li)>', summary, re.I | re.S):
                text = ' '.join(unescape(re.sub(r'<[^>]*>', ' ', block)).split())
                if _has_publisher_reporting(text, title, minimum_words=6) and complete_description(text):
                    if sum(map(len, paragraphs)) + len(text) > 12000:
                        break
                    paragraphs.append(text)
            if paragraphs:
                body = '\n\n'.join(paragraphs)

        if feed_url not in {PUBLIC_FEED, AXIOS_FEED} and body.rstrip().endswith(('...', '…')):
            # Plain RSS teasers can contain complete reporting followed by a
            # cut-off last sentence. Keep an unchanged complete prefix only.
            endings = list(re.finditer(r'(?<!\.)[.!?](?=\s)', body))
            for ending in reversed(endings):
                prefix = body[:ending.end()]
                if re.search(r'\b(?:[A-Z]\.){2,}$|\b(?:Mr|Mrs|Ms|Dr|Prof|St)\.$', prefix):
                    continue
                if len(prefix.split()) >= 6:
                    body = prefix
                    break
        # FT's public RSS standfirsts omit terminal punctuation by style.
        # Preserve the verbatim text; it must go through model extraction and
        # isolated claim review, never the complete-short-quotation shortcut.
        ft_standfirst = (feed_url in {_FEEDS['ft.com'], *_FT_TOPIC_FEEDS.values()} and len(body.split()) >= 6
                         and not re.search(r'(?:\.\s*){3}|…', body)
                         and not re.match(r'^(read|subscribe|sign in|click)\b', body, re.I))
        if not _has_publisher_reporting(body, title, minimum_words=6) or not (complete_description(body) or ft_standfirst):
            continue
        return (f'{PUBLIC_DESCRIPTION_PREFIX}\nHeadline: {title}\n'
                f'Original: {entry["link"]}\nPublic RSS: {feed_url}\n'
                f'Published: {stamp.isoformat()}\nExcerpt:\n{body[:12000]}')
    return None


async def public_feed_excerpt(article_url: str, source: str, *, topic: str = '') -> str | None:
    kwargs = {'topic': topic} if topic else {}
    return (await public_feed_excerpts([article_url], source, **kwargs)).get(article_url)


async def public_feed_excerpts(article_urls: list[str], source: str, *, topic: str = '') -> dict[str, str]:
    """Read bounded public feeds for up to three exact publisher URLs.

    Discovery candidates still require the caller to verify topic relevance.
    No article-page requests, redirects, credentials or access-control workarounds.
    Failure/missing descriptions leave the existing single search fallback intact.
    """
    domains = publisher_search_domains(source)
    if len(domains) != 1 or domains[0] not in _FEEDS:
        return {}
    host = domains[0]
    feeds = [_FEEDS[host]]
    if host == 'ft.com':
        selected = next((url for pattern, url in _FT_TOPIC_FEEDS.items()
                         if re.search(pattern, topic, re.I)), None)
        if selected:
            feeds.insert(0, selected)
    valid = []
    for article_url in dict.fromkeys(article_urls[:3]):
        identity = _article_identity(article_url)
        if (identity and article_url.startswith('https://') and identity[0] == host
                and identity[1] and not identity[2]):
            valid.append(article_url)
    if not valid:
        return {}
    excerpts = {}
    try:
        # Topic + home fallback share the existing deadline and byte budget.
        async with asyncio.timeout(FEED_TIMEOUT):
            async with httpx.AsyncClient(timeout=FEED_TIMEOUT, follow_redirects=False) as client:
                remaining = MAX_FEED_BYTES
                for feed_url in feeds:
                    async with client.stream('GET', feed_url) as response:
                        content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                        if response.status_code != 200 or not content_type.endswith(('/xml', '+xml')):
                            continue
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            remaining -= len(chunk)
                            if remaining < 0:
                                return excerpts
                            data.extend(chunk)
                    now = datetime.now(timezone.utc)
                    for url in valid:
                        if url not in excerpts:
                            text = _feed_article_excerpt(bytes(data), url, now, feed_url=feed_url)
                            if text:
                                excerpts[url] = text
                    if len(excerpts) == len(valid):
                        break
        return excerpts
    except Exception as exc:
        logger.info('compare public publisher feed unavailable (%s); using available originals/search', type(exc).__name__)
        return excerpts
