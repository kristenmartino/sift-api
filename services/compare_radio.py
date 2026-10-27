"""Bounded, on-demand discovery of public Reuters originals at WTVB.

The publisher advertises its monthly sitemaps in robots.txt. Search forms,
metadata snippets and Reuters photo credits never supply article evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from services.compare_search import normalize_evidence_text

logger = logging.getLogger('sift-api.compare')
INDEX = 'https://wtvbam.com/sitemap.xml'
TIMEOUT = 5
MAX_BYTES = 2_000_000
# Parsed URL metadata only; populated by reader requests, never a timer or DB poll.
_cache: dict[str, tuple[float, list[str]]] = {}
_STOP = set(('the and for from with about over into us of to a an in on at by as is are be '
             'can could will would may should has have had that this its it not no new says said '
             'after before under why how what which their').split())


def article_url(url: str) -> str | None:
    try:
        p = urlparse(url)
        if (p.scheme == 'https' and p.hostname in {'wtvbam.com', 'd2233.cms.socastsrm.com'}
                and p.port in {None, 443} and not p.username and not p.password
                and not p.query and not p.fragment
                and re.fullmatch(r'/20\d{2}/\d{2}/\d{2}/[a-z0-9-]+/', p.path)):
            # The publisher's sitemap uses both its CMS and public host.
            # Only request the public host and require its exact canonical URL.
            return 'https://wtvbam.com' + p.path
    except (TypeError, ValueError):
        pass
    return None


def _xml(data: bytes):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsupported XML declarations')
    return ET.fromstring(data)


async def _read(client, url, kind):
    async with client.stream('GET', url) as r:
        if r.status_code != 200 or r.headers.get('content-type', '').split(';')[0] not in kind:
            logger.info('compare Reuters WTVB public response rejected: url=%s status=%s type=%s',
                url, r.status_code, r.headers.get('content-type'))
            return None
        data = bytearray()
        async for chunk in r.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                logger.info('compare Reuters WTVB public response exceeded byte limit: url=%s', url)
                return None
        return bytes(data)


class _Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.schemas, self.body, self.byline, self.title = [], [], [], []
        self.canonical = None
        self.stack = []
        self.script = None
        self.byline_finished = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'link' and a.get('rel') == 'canonical':
            self.canonical = a.get('href')
        if tag == 'script' and a.get('type') == 'application/ld+json':
            self.script = []
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            classes = set(a.get('class', '').split())
            if ('hidden' in a or a.get('aria-hidden') == 'true'
                    or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', a.get('style', ''), re.I)):
                classes.add('_hidden')
            self.stack.append((tag, classes))
        if tag == 'p' and any(t == 'article' and 'mainArticle' in c for t, c in self.stack):
            self.body.append('\n')

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.schemas.append(json.loads(''.join(self.script)))
            except ValueError:
                pass
            self.script = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if any('sc-author' in c for _, c in self.stack[i:]):
                    self.byline_finished = True
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if any(t in {'script', 'style', 'nav', 'figure', 'footer'} or '_hidden' in c for t, c in self.stack):
            return
        if any(t == 'article' and 'mainArticle' in c for t, c in self.stack):
            self.body.append(data)
        if not self.byline_finished and any('sc-author' in c for _, c in self.stack):
            self.byline.append(data)
        if any(t == 'h1' for t, _ in self.stack):
            self.title.append(data)


def reporting(data: bytes, url: str, now: datetime) -> dict | None:
    if article_url(url) != url:
        return None
    page = _Article()
    page.feed(data.decode('utf-8'))
    if page.canonical != url or ''.join(page.byline).strip() != 'By Thomson Reuters':
        return None
    body = ''.join(c for c in ''.join(page.body) if unicodedata.category(c) != 'Cf').strip()
    if len(body.split()) < 60 or not re.search(r'\(Reuters\)\s*[-–—]', body[:600]):
        return None
    for schema in page.schemas:
        if not isinstance(schema, dict) or schema.get('@type') != 'NewsArticle':
            continue
        authors = schema.get('author')
        if not isinstance(authors, list) or not any(
                a.get('name') == 'Thomson Reuters' for a in authors if isinstance(a, dict)):
            continue
        try:
            stamp = datetime.fromisoformat(schema['datePublished'].replace('Z', '+00:00'))
            if not stamp.tzinfo or not now - timedelta(days=365) <= stamp <= now:
                continue
            if stamp.date().isoformat().replace('-', '/') != urlparse(url).path[1:11]:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        title = schema.get('headline')
        if not isinstance(title, str) or normalize_evidence_text(title) != normalize_evidence_text(''.join(page.title)):
            continue
        return {'title': title, 'url': url, 'excerpt':
            'Retrieved publisher excerpts (may be incomplete).\nPublisher: Reuters\nRepublished by: WTVB\n'
            f'Headline: {title}\nOriginal: {url}\nPublished: {stamp.isoformat()}\nExcerpt:\n{body[:4000]}'}
    return None


async def radio_candidates(topic: str, *, context: str | None = None) -> list[dict]:
    """At most 13 month maps and 3 public article pages, within five seconds.

    Sitemap paths rank discovery hints only. The existing budgeted semantic
    selection must still approve the full topic, event and timing.
    """
    from services.compare_syndication import _archive_query
    words = set(re.findall(r'[a-z0-9]+', _archive_query(topic).casefold())) - _STOP
    if len(words) < 2:
        return []
    # Another publisher's reviewed/public original can locate an event hidden
    # behind an editorial headline. It never supplies this outlet's evidence.
    context_words = set(re.findall(r'[a-z0-9]+',
        (topic + ' ' + context.partition('Excerpt:\n')[2]).casefold())) - _STOP if context else set()
    context_names = {w.casefold() for w in re.findall(r'\b[A-Z][a-zA-Z]+\b',
        context.partition('Excerpt:\n')[2])} - _STOP if context else set()
    topic_words = set(re.findall(r'[a-z0-9]+', topic.casefold())) - _STOP
    found, attempted = [], set()
    try:
        async with asyncio.timeout(TIMEOUT):
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
                now = datetime.now(timezone.utc)
                data = await _read(client, INDEX, {'text/xml', 'application/xml'})
                if not data:
                    return []
                maps = []
                for node in _xml(data):
                    url = node.findtext('{*}loc') or ''
                    m = re.fullmatch(r'https://wtvbam\.com/sitemap-post\.(20\d{2})(\d{2})\.xml', url)
                    if m:
                        try:
                            stamp = datetime(int(m[1]), int(m[2]), 1, tzinfo=timezone.utc)
                        except ValueError:
                            continue
                        if (now - timedelta(days=396)).replace(day=1) <= stamp <= now:
                            maps.append(url)
                for sitemap in sorted(set(maps), reverse=True)[:13]:
                    cached = _cache.get(sitemap)
                    if cached and time.monotonic() - cached[0] < 300:
                        urls = cached[1]
                    else:
                        data = await _read(client, sitemap, {'text/xml', 'application/xml'})
                        if not data:
                            continue
                        try:
                            urls = list(dict.fromkeys(u for n in _xml(data)
                                if (u := article_url(n.findtext('{*}loc') or ''))))
                        except (ET.ParseError, ValueError):
                            continue
                        _cache[sitemap] = (time.monotonic(), urls)
                        while len(_cache) > 13:
                            _cache.pop(next(iter(_cache)))
                    ranked = []
                    for url in urls:
                        if url in attempted:
                            continue
                        title_words = set(urlparse(url).path.rsplit('/', 2)[1].split('-'))
                        overlap = len(words & title_words)
                        context_overlap = len(context_words & title_words)
                        matches = (bool(context_names & title_words)
                            and len(topic_words & title_words) >= 2 and context_overlap >= 3
                            if context_names else overlap >= 2 and overlap / len(words) >= 0.5)
                        if matches:
                            ranked.append((max(overlap, context_overlap), url))
                    for _, url in sorted(ranked, reverse=True):
                        if len(attempted) >= 3:
                            return found
                        attempted.add(url)
                        try:
                            data = await _read(client, url, {'text/html'})
                            item = reporting(data, url, now) if data else None
                            if item:
                                found.append(item)
                        except Exception as exc:
                            logger.info('compare Reuters WTVB page rejected (%s)', type(exc).__name__)
                    if found:
                        return found
                logger.info('compare Reuters WTVB discovery completed: months=%d article_reads=%d originals=%d',
                    min(len(set(maps)), 13), len(attempted), len(found))
    except Exception as exc:
        logger.info('compare Reuters WTVB discovery ended (%s), retaining %d originals', type(exc).__name__, len(found))
    return found
