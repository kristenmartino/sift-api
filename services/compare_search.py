"""Publisher-scoped search and evidence from provider-owned citations."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

# Content hosts, rather than feed/CDN/rating-service hosts. Unknown publishers
# retain the name-based fallback; never guess a domain from the outlet's name.
_DOMAINS = {
    'axios': ('axios.com',), 'npr': ('npr.org',), 'pbsnewshour': ('pbs.org',),
    'washingtonexaminer': ('washingtonexaminer.com',), 'cbsnews': ('cbsnews.com',),
    'foxnews': ('foxnews.com',), 'newyorktimes': ('nytimes.com',),
    'dailywire': ('dailywire.com',), 'newyorkpost': ('nypost.com',),
    'hill': ('thehill.com',), 'wallstreetjournal': ('wsj.com',),
    'arstechnica': ('arstechnica.com',), 'verge': ('theverge.com',),
    'wired': ('wired.com',), 'cnbc': ('cnbc.com',),
    'nationalreview': ('nationalreview.com',), 'reuters': ('reuters.com',),
    'bloomberg': ('bloomberg.com',), 'bloombergnews': ('bloomberg.com',),
    'bbc': ('bbc.com', 'bbc.co.uk'), 'bbcnews': ('bbc.com', 'bbc.co.uk'),
    'bbcworld': ('bbc.com', 'bbc.co.uk'), 'associatedpress': ('apnews.com',),
    'apnews': ('apnews.com',), 'ap': ('apnews.com',),
    'guardian': ('theguardian.com',), 'guardianus': ('theguardian.com',),
    'washingtonpost': ('washingtonpost.com',),
}


def publisher_domains(source: str) -> tuple[str, ...]:
    name = re.sub(r'^the[ -]+', '', source.strip().casefold())
    return _DOMAINS.get(re.sub(r'[^a-z0-9]', '', name), ())


def _article_identity(url: str) -> tuple | None:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').removeprefix('www.')
        path = parsed.path.rstrip('/').removesuffix('/amp')
        query = tuple(sorted((k, v) for k, v in parse_qsl(parsed.query)
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
