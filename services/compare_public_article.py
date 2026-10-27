"""Normal public publisher pages: visible reporting only, never paywall bodies."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

import httpx

from services.compare_search import _article_identity, normalize_evidence_text

logger = logging.getLogger('sift-api.compare')
# Only publishers exercised against normal public HTTP responses.
_HOSTS = {'cbs news': 'cbsnews.com', 'wired': 'wired.com'}
MAX_BYTES = 2_000_000
# Advertised publisher archive, verified through its article topic links.
_WIRED_ARCHIVES = {'data brokers': 'https://www.wired.com/tag/data-brokers/'}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.paragraphs = []
        self.paragraph = None
        self.schemas = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (tag in {'script', 'style', 'template', 'noscript'} or 'hidden' in attrs
                  or attrs.get('aria-hidden') == 'true'
                  or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I)))
        inherited = self.stack[-1][1] if self.stack else False
        article = tag == 'article' or (self.stack[-1][2] if self.stack else False)
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script = []
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, hidden or inherited, article))
        if tag == 'p' and article and not (hidden or inherited):
            self.paragraph = []

    def handle_endtag(self, tag):
        if tag == 'p' and self.paragraph is not None:
            self.paragraphs.append(''.join(self.paragraph))
            self.paragraph = None
        if tag == 'script' and self.script is not None:
            self.schemas.append(''.join(self.script))
            self.script = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.paragraph is not None and self.stack and self.stack[-1][2] and not self.stack[-1][1]:
            self.paragraph.append(data)


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def article_excerpt(html: str, url: str, source: str, now: datetime) -> str | None:
    host = _HOSTS.get(source.strip().casefold())
    parsed_url = urlparse(url)
    if (not host or parsed_url.scheme != 'https' or parsed_url.hostname not in {host, 'www.' + host}
            or parsed_url.port not in (None, 443) or parsed_url.username or parsed_url.password):
        return None
    page = _Page()
    page.feed(html)
    for raw in page.schemas:
        try:
            schemas = list(_objects(json.loads(raw)))
        except (ValueError, TypeError):
            continue
        for schema in schemas:
            types = schema.get('@type', [])
            if isinstance(types, str):
                types = [types]
            if not any(t in {'NewsArticle', 'Article'} for t in types):
                continue
            if schema.get('isAccessibleForFree') in (False, 'false'):
                continue
            if any(part.get('isAccessibleForFree') in (False, 'false') for part in _objects(schema.get('hasPart'))):
                continue
            if _article_identity(schema.get('url', '')) != _article_identity(url) or not schema.get('author'):
                continue
            try:
                stamp = datetime.fromisoformat(schema['datePublished'].replace('Z', '+00:00'))
                if stamp.tzinfo is None or not now - timedelta(days=365) <= stamp <= now:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            body = schema.get('articleBody')
            if not isinstance(body, str):
                continue
            # Whole visible paragraphs must also occur in the publisher's
            # article body. Do not split at abbreviation periods or join a
            # truncated fragment to a different paragraph.
            normalized_body = normalize_evidence_text(body)
            retained = []
            for paragraph in page.paragraphs:
                paragraph = ' '.join(paragraph.split())
                if (len(paragraph.split()) >= 6 and paragraph.rstrip('“”"\'').endswith(('.', '!', '?'))
                        and not paragraph.rstrip().endswith(('...', '…'))
                        and normalize_evidence_text(paragraph) in normalized_body):
                    if sum(map(len, retained)) + len(paragraph) > 12000:
                        break
                    retained.append(paragraph)
            if sum(len(s.split()) for s in retained) < 40:
                continue
            return (f'Retrieved publisher excerpts (may be incomplete).\nOriginal: {url}\n'
                    f'Published: {stamp.isoformat()}\nExcerpt:\n' + '\n\n'.join(retained))
    return None


async def _get(client, url):
    async with client.stream('GET', url) as response:
        if response.status_code != 200 or 'text/html' not in response.headers.get('content-type', ''):
            return None
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                return None
        return data.decode('utf-8', errors='replace')


async def public_article_excerpt(url: str, source: str) -> str | None:
    host = _HOSTS.get(source.strip().casefold())
    parsed = urlparse(url)
    if (not host or parsed.scheme != 'https' or parsed.hostname not in {host, 'www.' + host}
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        return None
    try:
        async with asyncio.timeout(3):
            async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
                html = await _get(client, url)
        return article_excerpt(html, url, source, datetime.now(timezone.utc)) if html else None
    except Exception as exc:
        logger.info('compare public article unavailable: %s (%s)', source, type(exc).__name__)
        return None


async def public_article_candidates(topic: str, source: str) -> list[dict]:
    """Publisher's own public search supplies hints, never claims or relevance."""
    if source.strip().casefold() != 'wired':
        return []
    subject = re.split(r"[:?!]|\b(?:don't|doesn't|isn't|aren't)\b", topic, maxsplit=1, flags=re.I)[0]
    words = re.findall(r'[\w-]+', subject)[:6]
    if len(words) < 2:
        words = re.findall(r'[\w-]+', topic)[:6]
    archive = next((url for phrase, url in _WIRED_ARCHIVES.items()
                    if re.search(r'\b' + re.escape(phrase) + r'\b', topic, re.I)), None)
    discovery_url = archive or 'https://www.wired.com/search/?q=' + quote(' '.join(words))
    candidates = []
    try:
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
                html = await _get(client, discovery_url)
                if not html:
                    return []
                paths = list(dict.fromkeys(re.findall(r'/story/[a-z0-9-]+/', html)))[:2]
                for path in paths:
                    url = 'https://www.wired.com' + path
                    article = await _get(client, url)
                    excerpt = article_excerpt(article, url, source, datetime.now(timezone.utc)) if article else None
                    if excerpt:
                        candidates.append({'title': path.split('/')[-2].replace('-', ' '), 'url': url, 'excerpt': excerpt})
                return candidates
    except Exception as exc:
        logger.info('compare publisher discovery unavailable: %s (%s)', source, type(exc).__name__)
        return candidates
