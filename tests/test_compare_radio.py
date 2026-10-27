import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import httpx
import pytest

from services import compare_radio as radio, compare_syndication as archive

NOW = datetime(2026, 10, 27, 19, tzinfo=timezone.utc)
URL = 'https://wtvbam.com/2026/10/26/us-20-states-sue-dollar-general-for-alleged-overcharges/'
BODY = ('Oct 26 (Reuters) – The agency and twenty states alleged that the retailer overcharged customers. '
        'The allegations have not been decided by a court. The retailer did not immediately respond to requests '
        'for comment on the lawsuit. The complaint says the company had received numerous consumer complaints '
        'about shelf prices and checkout prices. The states asked the court to order relief for affected shoppers.')
TITLE = 'US, 20 states sue Dollar General for alleged overcharges'


def page(**changes):
    opts = {'canonical': URL, 'author': 'Thomson Reuters', 'byline': 'By Thomson Reuters',
            'date': '2026-10-26T17:47:00+00:00', 'visible': BODY, 'headline': TITLE}
    opts.update(changes)
    schema = {'@type': 'NewsArticle', 'headline': opts['headline'], 'datePublished': opts['date'],
              'author': [{'name': opts['author']}]}
    return (f'<link rel="canonical" href="{opts["canonical"]}">'
            f'<script type="application/ld+json">{json.dumps(schema)}</script>'
            f'<h1>{TITLE}</h1><span class="sc-author">{opts["byline"]}</span>'
            f'<article class="mainArticle"><p>{opts["visible"]}</p></article>'
            '<article>Related articles are not reporting from the selected page.</article>').encode()


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(radio, 'datetime', Frozen)
    radio._cache.clear()
    yield
    radio._cache.clear()


@pytest.mark.parametrize('url', [URL.replace('wtvbam.com', 'wtvbam.com.evil.example'),
    URL.replace('https://', 'http://'), URL.replace('wtvbam.com', 'user:pass@wtvbam.com'),
    URL + '?redirect=evil', URL + '#comments', URL.replace('/2026/', '/1999/')])
def test_only_exact_public_article_routes_can_be_fetched(url):
    assert radio.article_url(url) is None


def test_publisher_sitemap_cms_alias_normalizes_to_public_host_only():
    assert radio.article_url(URL.replace('wtvbam.com', 'd2233.cms.socastsrm.com')) == URL
    assert radio.article_url(URL.replace('wtvbam.com', 'd9999.cms.socastsrm.com')) is None


@pytest.mark.parametrize('changes', [
    {'canonical': URL + 'other/'}, {'author': 'Staff; photo Reuters'}, {'byline': 'By Associated Press'},
    {'date': '2024-10-26T17:00:00Z'}, {'date': '2026-10-28T17:00:00Z'},
    {'date': '2026-10-25T17:00:00Z'}, {'date': '2026-10-26T17:00:00'},
    {'visible': 'Subscribe to read more.'}, {'headline': 'Different article'},
    {'visible': BODY.replace('(Reuters)', '(Staff)')},
    {'visible': '<span hidden>' + BODY + '</span>'},
    {'visible': '<span style="display: none">' + BODY + '</span>'},
    {'byline': 'By Staff<span class="sc-author">By Thomson Reuters</span>'},
])
def test_metadata_photo_credits_access_prompts_hidden_copy_and_wrong_dates_cannot_supply_claims(changes):
    assert radio.reporting(page(**changes), URL, NOW) is None


def test_only_visible_article_body_under_verified_authorship_is_returned():
    result = radio.reporting(page(visible=BODY + '<script>Invented numbers</script>'), URL, NOW)
    assert BODY in result['excerpt']
    assert 'Invented' not in result['excerpt']
    assert 'Related articles' not in result['excerpt']
    assert 'alleged' in result['excerpt']


def xml(tag, urls):
    return ('<' + tag + '>' + ''.join('<item><loc>' + u + '</loc></item>' for u in urls) + '</' + tag + '>').encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['empty', 'malformed', 'untrusted', 'oversized'])
async def test_older_months_recover_originals_without_search_forms_or_metadata_as_evidence(monkeypatch, first):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(radio, 'datetime', Frozen)
    seen = []
    def serve(req):
        u = str(req.url)
        seen.append(u)
        if u == radio.INDEX:
            return httpx.Response(200, content=xml('sitemapindex', [
                'https://evil.example/sitemap-post.202610.xml', 'https://wtvbam.com/sitemap-post.202613.xml',
                'https://wtvbam.com/sitemap-post.202610.xml', 'https://wtvbam.com/sitemap-post.202609.xml']), headers={'content-type': 'text/xml'})
        if u.endswith('202610.xml'):
            data = {'empty': xml('urlset', []), 'malformed': b'bad xml', 'untrusted': xml('urlset', [URL.replace('wtvbam.com', 'evil.example')]), 'oversized': b'x' * (radio.MAX_BYTES + 1)}[first]
            return httpx.Response(200, content=data, headers={'content-type': 'text/xml'})
        if u.endswith('202609.xml'):
            return httpx.Response(200, content=xml('urlset', [URL.replace('wtvbam.com', 'd2233.cms.socastsrm.com')]), headers={'content-type': 'application/xml'})
        assert u == URL
        return httpx.Response(200, content=page(), headers={'content-type': 'text/html'})
    actual = httpx.AsyncClient
    monkeypatch.setattr(radio.httpx, 'AsyncClient', lambda **kw: actual(**kw, transport=httpx.MockTransport(serve)))
    result = await radio.radio_candidates('Dollar General')
    assert result[0]['url'] == URL and BODY in result[0]['excerpt']
    assert len(seen) == 4
    assert not any('?s=' in u for u in seen)


@pytest.mark.asyncio
async def test_verified_ksl_originals_skip_the_additional_adapter(monkeypatch):
    candidate = {'url': URL, 'excerpt': BODY}
    monkeypatch.setattr(archive, '_ksl_archive_candidates', AsyncMock(return_value=[candidate]))
    fallback = AsyncMock()
    monkeypatch.setattr(radio, 'radio_candidates', fallback)
    assert await archive.reuters_archive_candidates('Dollar General') == [candidate]
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_cited_radio_passages_require_exact_support_in_verified_visible_body(monkeypatch):
    actual = httpx.AsyncClient
    monkeypatch.setattr(archive.httpx, 'AsyncClient', lambda **kw: actual(**kw, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=page(), headers={'content-type': 'text/html'}))))
    quote = 'The allegations have not been decided by a court.'
    def response(q):
        return Obj(content=[Obj(type='text', citations=[Obj(type='web_search_result_location', url=URL, cited_text=q)])])
    result = await archive.reuters_partner_excerpts(response(quote))
    assert quote in result
    assert await archive.reuters_partner_excerpts(response('The company has admitted guilt and promised to repay all shoppers.')) is None


@pytest.mark.asyncio
async def test_shared_deadline_preserves_a_completed_original_and_cancels_slow_sibling(monkeypatch):
    monkeypatch.setattr(radio, 'TIMEOUT', 0.04)
    slow = URL.replace('us-20-states', 'another-20-states')
    cancelled = []
    async def serve(req):
        u = str(req.url)
        if u == radio.INDEX:
            return httpx.Response(200, content=xml('sitemapindex', ['https://wtvbam.com/sitemap-post.202610.xml']), headers={'content-type': 'text/xml'})
        if u.endswith('.xml'):
            return httpx.Response(200, content=xml('urlset', [URL, slow]), headers={'content-type': 'text/xml'})
        if u == URL:
            return httpx.Response(200, content=page(), headers={'content-type': 'text/html'})
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    actual = httpx.AsyncClient
    monkeypatch.setattr(radio.httpx, 'AsyncClient', lambda **kw: actual(**kw, transport=httpx.MockTransport(serve)))
    result = await radio.radio_candidates('Dollar General')
    assert result[0]['url'] == URL and cancelled


@pytest.mark.asyncio
async def test_metadata_scan_is_bounded_and_cached_only_on_demand(monkeypatch):
    calls = []
    months = [f'https://wtvbam.com/sitemap-post.{year}{month:02d}.xml'
              for year in [2026, 2025] for month in range(1, 13)]
    def serve(req):
        u = str(req.url)
        calls.append(u)
        return httpx.Response(200, content=xml('sitemapindex', months) if u == radio.INDEX else xml('urlset', []),
                              headers={'content-type': 'text/xml'})
    actual = httpx.AsyncClient
    def client(**kw):
        assert kw['follow_redirects'] is False
        return actual(**kw, transport=httpx.MockTransport(serve))
    monkeypatch.setattr(radio.httpx, 'AsyncClient', client)
    assert await radio.radio_candidates('Dollar General') == []
    assert len(calls) == 14 and len(radio._cache) == 13
    calls.clear()
    assert await radio.radio_candidates('Dollar General') == []
    assert calls == [radio.INDEX]


@pytest.mark.asyncio
async def test_article_read_limit_applies_even_when_every_page_is_unavailable(monkeypatch):
    calls = []
    urls = [URL.replace('us-20-states', f'case-{i}') for i in range(10)]
    def serve(req):
        u = str(req.url)
        calls.append(u)
        if u == radio.INDEX:
            return httpx.Response(200, content=xml('sitemapindex', ['https://wtvbam.com/sitemap-post.202610.xml']), headers={'content-type': 'text/xml'})
        if u.endswith('.xml'):
            return httpx.Response(200, content=xml('urlset', urls), headers={'content-type': 'text/xml'})
        return httpx.Response(403)
    actual = httpx.AsyncClient
    monkeypatch.setattr(radio.httpx, 'AsyncClient', lambda **kw: actual(**kw, transport=httpx.MockTransport(serve)))
    assert await radio.radio_candidates('Dollar General') == []
    assert len([u for u in calls if radio.article_url(u)]) == 3


@pytest.mark.asyncio
async def test_editorial_context_only_discovers_a_separately_verified_original(monkeypatch):
    def serve(req):
        u = str(req.url)
        if u == radio.INDEX:
            return httpx.Response(200, content=xml('sitemapindex', ['https://wtvbam.com/sitemap-post.202610.xml']), headers={'content-type': 'text/xml'})
        if u.endswith('.xml'):
            return httpx.Response(200, content=xml('urlset', [URL]), headers={'content-type': 'text/xml'})
        assert u == URL
        return httpx.Response(200, content=page(), headers={'content-type': 'text/html'})
    actual = httpx.AsyncClient
    monkeypatch.setattr(radio.httpx, 'AsyncClient', lambda **kw: actual(**kw, transport=httpx.MockTransport(serve)))
    topic = 'States sue alleging violations of fundamental consumer protection and privacy rights'
    assert await radio.radio_candidates(topic) == []
    result = await radio.radio_candidates(topic, context='Original: https://other.example/\nExcerpt:\nA dispute involves Dollar General and states. Other publisher alleges imaginary losses.')
    assert result[0]['url'] == URL
    assert 'imaginary losses' not in result[0]['excerpt']


@pytest.mark.asyncio
async def test_workflow_passes_editorial_context_only_for_discovery_not_claim_evidence(monkeypatch):
    from workflows import compare_workflow as workflow
    from unittest.mock import Mock
    context = 'Public description\nExcerpt:\nA Colorado case concerns religious funding.'
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(side_effect=[None,
        {'url': 'https://nationalreview.com/case/', 'title': 'Editorial headline', 'excerpt': context}]))
    evidence = 'Retrieved publisher excerpts.\nExcerpt:\n' + BODY
    discovery = AsyncMock(return_value=[{'url': URL, 'title': TITLE, 'excerpt': evidence}])
    monkeypatch.setattr(workflow, 'reuters_archive_candidates', discovery)
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=Obj(content=[Obj(type='text', text='SELECTED_STORED_CANDIDATE=0')])))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    _, result = await workflow.search_one_source(Mock(), 'Editorial headline', 'Reuters')
    discovery.assert_awaited_once_with('Editorial headline', context=context)
    assert result == evidence and context not in result
