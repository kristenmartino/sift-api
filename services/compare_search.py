"""Publisher-scoped search and evidence from provider-owned citations."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlparse

# Verified publisher-specific adapter. Some publishers (including Reuters)
# reject provider allowed_domains filters, so they retain name-based search.
# Extend only after exercising the publisher with the live provider.
_DOMAINS = {
    'nationalreview': ('nationalreview.com',),
    'hill': ('thehill.com',),
    'cnbc': ('cnbc.com',),
}
_SEARCH_DOMAINS = {
    **_DOMAINS,
    'reuters': ('reuters.com',),
    'foxnews': ('foxnews.com',),
    'cbsnews': ('cbsnews.com',),
    'npr': ('npr.org',),
    'financialtimes': ('ft.com',),
    'hill': ('thehill.com',),
    'axios': ('axios.com',),
    'verge': ('theverge.com',),
    'wired': ('wired.com',),
    'arstechnica': ('arstechnica.com',),
    'cnbc': ('cnbc.com',),
}


def normalize_evidence_text(text: str) -> str:
    """Ignore typography without changing words, numbers or attribution."""
    text = unicodedata.normalize('NFKC', text).translate(str.maketrans({
        '’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-',
    }))
    # Publishers and model quotes can use single/double marks around the
    # same term. Normalize paired quotation marks only; preserve apostrophes
    # inside contractions and possessives, words, numbers and clause order.
    text = re.sub(r"(?<!\w)'([^'\n]+)'(?!\w)", r'"\1"', text)
    return ' '.join(text.casefold().split())


def publisher_domains(source: str) -> tuple[str, ...]:
    name = re.sub(r'^the[ -]+', '', source.strip().casefold())
    return _DOMAINS.get(re.sub(r'[^a-z0-9]', '', name), ())


def publisher_search_domains(source: str) -> tuple[str, ...]:
    """Verified query/citation sites, independent of provider filter support."""
    name = re.sub(r'^the[ -]+', '', source.strip().casefold())
    return _SEARCH_DOMAINS.get(re.sub(r'[^a-z0-9]', '', name), ())


def _article_identity(url: str) -> tuple | None:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').removeprefix('www.')
        path = parsed.path.rstrip('/').removesuffix('/amp')
        ft_content = host == 'ft.com' and re.fullmatch(r'/content/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', path)
        query = tuple(sorted((k, v) for k, v in parse_qsl(parsed.query)
                             if not (ft_content and k.casefold().startswith('syn-'))
                             if not k.casefold().startswith('utm_')
                             and k.casefold() not in {'amp', 'output', 'mod', 'ref', 'source', 'smid'}))
        return host, path, query
    except (TypeError, ValueError):
        return None


def publisher_search_excerpts(response, domains: tuple[str, ...], preferred_url: str | None = None) -> str | None:
    """Only provider citations can turn a scoped search into source evidence.

    Text mentioning an outlet or a URL, a search-result title, and an access
    error are not publisher reporting. Public search-index excerpts can be
    usable even when the article page rejects a direct server request.
    """
    # Search ages are provider metadata, never fabricated publication dates.
    # Enforce the same one-year window as discovery when an age is supplied.
    older_results = set()
    for block in response.content:
        if block.type != 'web_search_tool_result' or not isinstance(block.content, list):
            continue
        for result in block.content:
            age = re.fullmatch(r'(\d+) days? ago', getattr(result, 'page_age', '') or '')
            if age and int(age[1]) > 365:
                older_results.add(_article_identity(getattr(result, 'url', '')))
    passages = []
    urls = []
    for block in response.content:
        if block.type != 'text':
            continue
        for citation in getattr(block, 'citations', None) or []:
            if getattr(citation, 'type', '') != 'web_search_result_location':
                continue
            url = getattr(citation, 'url', '')
            try:
                parsed = urlparse(url)
                host = parsed.hostname or ''
            except (TypeError, ValueError):
                continue
            if parsed.scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in domains):
                continue
            # Forum posts are reader contributions, not Ars newsroom reporting.
            if host.removeprefix('www.') == 'arstechnica.com' and parsed.path.startswith('/civis/'):
                continue
            if _article_identity(url) in older_results:
                continue
            if preferred_url and _article_identity(url) != _article_identity(preferred_url):
                continue
            quote = getattr(citation, 'cited_text', '')
            if not isinstance(quote, str):
                continue
            quote = quote.strip()
            if len(quote.split()) < 8:
                continue
            normalized = ' '.join(re.findall(r'\w+', quote.casefold()))
            title = ' '.join(re.findall(r'\w+', (getattr(citation, 'title', '') or '').casefold()))
            if normalized == title or re.match(r'^(subscribe|sign in|sign up|log in|read more)\b', quote, re.I):
                continue
            if quote not in passages:
                passages.append(quote)
            if url not in urls:
                urls.append(url)
    if not passages:
        return None
    return ('Retrieved publisher excerpts (may be incomplete).\nOriginal: '
            + '\nOriginal: '.join(urls) + '\nExcerpt:\n' + '\n\n'.join(passages)[:12000])
