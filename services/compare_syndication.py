"""Verify cited Reuters republications on a deliberately narrow partner list."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from html import unescape
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from services.compare_search import _article_identity, normalize_evidence_text

logger = logging.getLogger('sift-api.compare')
PARTNER_DOMAIN = 'ksl.com'
VERIFY_TIMEOUT = 2
MAX_PAGE_BYTES = 1_000_000
ARCHIVE_URL = 'https://www.ksl.com/search/api'
ARCHIVE_TIMEOUT = 3


def _partner_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and parsed.hostname in {'ksl.com', 'www.ksl.com'}
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and bool(re.fullmatch(r'/article/\d+/[a-z0-9-]+/?', parsed.path)))
    except (TypeError, ValueError):
        return False


def _normalized(text: str) -> str:
    # KSL's body contains formatting-only zero-width characters. Removing
    # them changes no words, numbers or attribution.
    return normalize_evidence_text(''.join(c for c in text if unicodedata.category(c) != 'Cf'))


class _PublicArticle(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.schemas = []
        self.visible = []
        self.script = None
        self.in_article = False
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script = []
        if tag == 'article' and attrs.get('id') == 'kslMainArticle':
            self.in_article = True
        if self.in_article and tag in {'script', 'style', 'figure', 'nav'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.schemas.append(json.loads(''.join(self.script)))
            except ValueError:
                pass
            self.script = None
        if tag == 'article':
            self.in_article = False
        if self.in_article and tag in {'script', 'style', 'figure', 'nav'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.in_article and not self.hidden:
            self.visible.append(data)


def _verified_passages(data: bytes, url: str, quotes: list[str], now: datetime) -> str | None:
    page = _PublicArticle()
    page.feed(data.decode('utf-8'))
    for article in page.schemas:
        if not isinstance(article, dict) or article.get('@type') != 'NewsArticle':
            continue
        if _article_identity(article.get('url', '')) != _article_identity(url):
            continue
        # A Reuters photo credit, publisher mention or navigation link is not
        # authorship. This adapter requires the article's explicit byline.
        authors = article.get('author', [])
        if not isinstance(authors, list):
            authors = [authors]
        if not any(isinstance(a, dict) and isinstance(a.get('name'), str)
                   and re.fullmatch(r'(?:[^<>]{2,120},\s*)?Reuters', a['name'].strip(), re.I) for a in authors):
            continue
        try:
            stamp = datetime.fromisoformat(article['datePublished'].replace('Z', '+00:00'))
            if not stamp.tzinfo or not now - timedelta(days=365) <= stamp <= now:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        body = article.get('articleBody', '')
        if not isinstance(body, str):
            continue
        visible = _normalized(''.join(page.visible))
        body = _normalized(body)
        passages = [q for q in quotes if len(q.split()) >= 8
                    and _normalized(q) in body and _normalized(q) in visible
                    and _normalized(q) != _normalized(article.get('headline', ''))]
        if passages:
            return ('Retrieved publisher excerpts (may be incomplete).\nPublisher: Reuters\n'
                    f'Republished by: KSL\nOriginal: {url}\nPublished: {stamp.isoformat()}\n'
                    'Excerpt:\n' + '\n\n'.join(dict.fromkeys(passages))[:12000])
    return None


def _archive_query(topic: str) -> str:
    subject = topic.partition(':')[0].strip()
    # Editorial titles often introduce the subject with "The ... of ...".
    # Use the subject for discovery; the selection call sees the full title.
    if re.match(r'^(?:the|an?)\b', subject, re.I) and re.search(r'\bof\b', subject, re.I):
        subject = re.split(r'\bof\b', subject, flags=re.I)[-1]
    ipo = re.search(r'\b(?:IPO|initial public offering)\b', subject, re.I)
    if ipo:
        company = re.split(r'\b(?:files?|filed|filing|plans?|prepares?|for|as)\b', subject[:ipo.start()], flags=re.I)[0]
        company = re.sub(r'^(?:the|an?)\s+', '', company.strip(), flags=re.I)
        words = re.findall(r"[\w'-]+", company)[:6]
        if words:
            return ' '.join(words) + ' IPO'
    words = re.findall(r"[\w'-]+", subject)[:12]
    if len(words) > 6:
        # Long event headlines can overconstrain an archive's literal search.
        # Prefer a compact explicit named subject, never an inferred entity.
        phrases = re.findall(r"\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){1,3}\b", subject)
        phrases = [re.sub(r'^(?:FBI\s+)?(?:Director|President|Senator|Governor|Chief|CEO)\s+', '', p)
                   for p in phrases]
        phrases = [p for p in phrases if 2 <= len(p.split()) <= 4]
        if phrases:
            return max(phrases, key=lambda p: len(p.split()))[:120]
    while len(words) > 2 and words[-1].casefold() in {'reform', 'debate', 'analysis', 'explained'}:
        words.pop()
    return ' '.join(words)[:120]


def _archive_candidates(data: bytes, now: datetime) -> list[str]:
    """Archive ranks titles, including paid content; metadata is never evidence."""
    result = json.loads(data)
    urls, seen = [], set()
    for item in result.get('news', [])[:20]:
        if not isinstance(item, dict):
            continue
        title, url = item.get('title'), item.get('url')
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        try:
            stamp = datetime.strptime(item.get('date', ''), '%b %d, %Y').replace(tzinfo=timezone.utc)
            if not now - timedelta(days=365) <= stamp <= now:
                continue
            parsed = urlparse(url)
            # The archive uses slug/id links; canonical article pages use
            # id/slug. Normalize only that documented route, never redirects.
            match = re.fullmatch(r'/article/([a-z0-9-]+)/(\d+)/?', parsed.path)
            if (parsed.scheme != 'https' or parsed.hostname not in {'ksl.com', 'www.ksl.com'}
                    or parsed.port not in {None, 443} or parsed.username or parsed.password
                    or not match or match[2] != str(item.get('id'))):
                continue
        except (TypeError, ValueError):
            continue
        key = normalize_evidence_text(re.sub(r'^COPY OF\s*-\s*', '', unescape(title), flags=re.I))
        if key in seen:
            continue
        seen.add(key)
        urls.append(f'https://www.ksl.com/article/{match[2]}/{match[1]}')
        if len(urls) == 3:
            break
    return urls


async def _public_bytes(client, url, content_type, **kwargs):
    async with client.stream('GET', url, **kwargs) as response:
        if response.status_code != 200 or response.headers.get('content-type', '').split(';')[0] != content_type:
            return None
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_PAGE_BYTES:
                return None
        return bytes(data)


def _archive_reporting(data: bytes, url: str, now: datetime) -> dict | None:
    page = _PublicArticle()
    page.feed(data.decode('utf-8'))
    for article in page.schemas:
        if (not isinstance(article, dict) or article.get('@type') != 'NewsArticle'
                or _article_identity(article.get('url', '')) != _article_identity(url)):
            continue
        body = article.get('articleBody')
        title = article.get('headline')
        if not isinstance(body, str) or not isinstance(title, str):
            continue
        # Reuse the same authorship, date, URL and visible-body verification.
        # Archive descriptions, sponsorship copy and photo credits add nothing.
        if _verified_passages(data, url, [body], now):
            body = ''.join(c for c in body if unicodedata.category(c) != 'Cf')
            excerpt = (f'Retrieved publisher excerpts (may be incomplete).\nPublisher: Reuters\n'
                       f'Republished by: KSL\nHeadline: {title}\nOriginal: {url}\n'
                       f'Published: {article["datePublished"]}\nExcerpt:\n{body[:4000]}')
            return {'title': title, 'url': url, 'excerpt': excerpt}
    return None


async def reuters_archive_candidates(topic: str) -> list[dict]:
    """One public archive lookup and at most three article reads, no paid search.

    A returned original excerpt still requires semantic event/timing selection
    by the existing budgeted Compare operation. No automatic keyword match.
    """
    query = _archive_query(topic)
    if not query:
        return []
    completed = {}
    try:
        async with asyncio.timeout(ARCHIVE_TIMEOUT):
            async with httpx.AsyncClient(timeout=ARCHIVE_TIMEOUT, follow_redirects=False) as client:
                data = await _public_bytes(client, ARCHIVE_URL, 'application/json',
                    params={'search': query, 'sort': 'relevance', 'page': '1'})
                if not data:
                    return []
                now = datetime.now(timezone.utc)
                urls = _archive_candidates(data, now)

                async def verify_one(index, url):
                    try:
                        page = await _public_bytes(client, url, 'text/html')
                        candidate = _archive_reporting(page, url, now) if page else None
                        if candidate:
                            completed[index] = candidate
                    except Exception as exc:
                        logger.info('compare Reuters candidate rejected (%s)', type(exc).__name__)

                tasks = [asyncio.create_task(verify_one(i, url)) for i, url in enumerate(urls)]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.info('compare Reuters archive ended (%s), retaining %d verified candidates', type(exc).__name__, len(completed))
    return [completed[i] for i in sorted(completed)]


async def reuters_partner_excerpts(response) -> str | None:
    """At most two public pages, inside one two-second verification deadline.

    Only model-selected provider citations are candidates; this does not
    promote every search hit. No redirects, credentials, access workarounds
    or extra paid searches. Verification failure leaves coverage unavailable.
    """
    candidates = {}
    for block in response.content:
        if block.type != 'text':
            continue
        for citation in getattr(block, 'citations', None) or []:
            url = getattr(citation, 'url', '')
            quote = getattr(citation, 'cited_text', '')
            if (getattr(citation, 'type', '') == 'web_search_result_location'
                    and _partner_url(url) and isinstance(quote, str) and len(quote.split()) >= 8):
                candidates.setdefault(url, []).append(quote.strip())
    if not candidates:
        return None
    try:
        async with asyncio.timeout(VERIFY_TIMEOUT):
            async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT, follow_redirects=False) as client:
                for url, quotes in list(candidates.items())[:2]:
                    async with client.stream('GET', url) as result:
                        if result.status_code != 200 or result.headers.get('content-type', '').split(';')[0] != 'text/html':
                            continue
                        data = bytearray()
                        async for chunk in result.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_PAGE_BYTES:
                                break
                        if len(data) > MAX_PAGE_BYTES:
                            continue
                    evidence = _verified_passages(bytes(data), url, quotes, datetime.now(timezone.utc))
                    if evidence:
                        return evidence
    except Exception as exc:
        logger.info('compare Reuters republication unverified (%s)', type(exc).__name__)
    return None
