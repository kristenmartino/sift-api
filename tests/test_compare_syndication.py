import json
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services import compare_syndication as syndication
from workflows import compare_workflow as workflow

URL = 'https://www.ksl.com/article/51508959/a-funding-report'
QUOTE = 'The fund would pay only a portion of scheduled benefits after reserves are depleted.'
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def archive_miss(monkeypatch):
    monkeypatch.setattr(workflow, 'reuters_archive_candidates', AsyncMock(return_value=[]))


def html(*, author='Reuters', body=QUOTE, visible=QUOTE, url=URL, stamp='2026-06-09T12:32:00-06:00'):
    schema = {'@type': 'NewsArticle', 'author': [{'name': author}], 'url': url,
              'datePublished': stamp, 'headline': 'A funding report', 'articleBody': body}
    return (f'<script type="application/ld+json">{json.dumps(schema)}</script>'
            f'<figure>Reuters photo: {QUOTE}</figure>'
            f'<article id="kslMainArticle"><p>{visible}</p></article>').encode()


def response(url=URL, quote=QUOTE, text='Reuters reports on the funding shortfall.'):
    return Obj(content=[Obj(type='text', text='COVERAGE_MATCH=YES\n' + text, citations=[Obj(type='web_search_result_location',
        url=url, cited_text=quote, title='A funding report')])])


def test_verified_republication_requires_byline_and_matching_visible_article_text():
    result = syndication._verified_passages(html(), URL, [QUOTE], NOW)
    assert QUOTE in result
    assert 'Publisher: Reuters\nRepublished by: KSL' in result
    assert 'Published: 2026-06-09' in result


@pytest.mark.parametrize('params', [
    {'author': 'KSL staff'}, {'body': 'Different article body.'}, {'visible': 'Different visible body.'},
    {'url': 'https://www.ksl.com/article/123/another-story'}, {'stamp': '2024-10-26T00:00:00+00:00'},
    {'stamp': '2026-10-28T00:00:00+00:00'}, {'stamp': '2026-10-26'}, {'stamp': 'invalid'},
])
def test_photo_credit_metadata_mismatch_old_or_future_text_cannot_supply_reuters_claims(params):
    assert syndication._verified_passages(html(**params), URL, [QUOTE], NOW) is None


def test_formatting_characters_do_not_change_passage_identity():
    assert syndication._verified_passages(html(body=QUOTE.replace('fund', 'f\u200bund'),
        visible=QUOTE.replace('fund', 'f\u200bund')), URL, [QUOTE], NOW)


@pytest.mark.parametrize('url', [
    'https://ksl.com.evil.example/article/123/story', 'https://other.example/article/123/story',
    'http://www.ksl.com/article/123/story', 'https://user:password@www.ksl.com/article/123/story',
    'https://www.ksl.com:8080/article/123/story', 'https://www.ksl.com/author/reuters',
])
@pytest.mark.asyncio
async def test_untrusted_hosts_non_article_urls_and_credentials_never_trigger_requests(monkeypatch, url):
    client = Mock()
    monkeypatch.setattr(syndication.httpx, 'AsyncClient', client)
    assert await syndication.reuters_partner_excerpts(response(url=url)) is None
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'body', 'ctype'), [
    (200, html(), 'text/html'), (302, html(), 'text/html'), (403, html(), 'text/html'),
    (200, html(), 'application/json'), (200, b'x' * 1_000_001, 'text/html'),
], ids=['valid', 'redirect', 'access-denied', 'not-html', 'oversized'])
async def test_bounded_public_verification_does_not_follow_redirects_or_access_failures(monkeypatch, status, body, ctype):
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers={'content-type': ctype})
    actual = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False
        return actual(**kwargs, transport=httpx.MockTransport(serve))
    monkeypatch.setattr(syndication.httpx, 'AsyncClient', client)
    result = await syndication.reuters_partner_excerpts(response())
    assert bool(result) is (status == 200 and ctype == 'text/html' and len(body) <= 1_000_000)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('source', 'stored', 'reply', 'accepted'), [
    ('Reuters', None, 'Reuters reports on the shortfall.', True),
    ('Fox News', None, 'Reuters reports on the shortfall.', False),
    ('Reuters', None, 'No relevant coverage found from Reuters.', False),
    ('Reuters', {'title': 'A different event', 'url': 'https://www.reuters.com/a-different-event/',
                 'excerpt': None}, 'Reuters reports on the shortfall.', False),
])
async def test_partner_selection_uses_existing_search_and_cannot_override_irrelevance_or_exact_event(monkeypatch, source, stored, reply, accepted):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=stored))
    monkeypatch.setattr(workflow, 'public_feed_excerpt', AsyncMock(return_value=None))
    paid = AsyncMock(return_value=response(text=reply))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    partner = AsyncMock(return_value='Retrieved publisher excerpts\nExcerpt:\n' + QUOTE)
    monkeypatch.setattr(workflow, 'reuters_partner_excerpts', partner)
    _, evidence = await workflow.search_one_source(Mock(), 'Social Security funding', source)
    assert bool(evidence) is accepted
    assert partner.await_count == int(accepted)
    paid.assert_awaited_once()
    assert paid.await_args.kwargs['tools'][0]['max_uses'] == 1
    assert 'allowed_domains' not in paid.await_args.kwargs['tools'][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(('topic', 'query'), [
    ('Social Security reform: benefit cuts versus tax increases', 'Reuters Social Security reform'),
    ('Explainer: Colorado preschool appeal', 'Reuters Explainer: Colorado preschool appeal'),
])
async def test_broad_subject_query_preserves_full_request_for_relevance_review(monkeypatch, topic, query):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    paid = AsyncMock(return_value=Obj(content=[]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    await workflow.search_one_source(Mock(), topic, 'Reuters')
    prompt = paid.await_args.kwargs['messages'][0]['content']
    assert f'targeted search query: {query}\n' in prompt
    assert f'about: "{topic}"' in prompt
