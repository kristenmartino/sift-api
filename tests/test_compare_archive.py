import json
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services import compare_syndication as archive
from workflows import compare_workflow as workflow

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
URL = 'https://www.ksl.com/article/123/a-funding-report'
BODY = 'The trustees reported a funding shortfall that could reduce scheduled retirement benefits in coming years.'


def page(author='Reuters', visible=BODY):
    data = {'@type': 'NewsArticle', 'author': [{'name': author}], 'url': URL,
            'headline': 'A funding report', 'datePublished': '2026-06-09T12:00:00+00:00', 'articleBody': BODY}
    return (f'<script type="application/ld+json">{json.dumps(data)}</script>'
            f'<article id="kslMainArticle"><p>{visible}</p></article>').encode()


def candidate(i=123, title='A funding report', **kw):
    return {'id': i, 'title': title, 'date': 'Jun 9, 2026',
            'url': f'https://ksl.com/article/a-funding-report/{i}', **kw}


@pytest.mark.parametrize(('topic', 'query'), [
    ('Social Security reform: benefit cuts versus tax increases', 'Social Security'),
    ('The Hidden Dividend of Social Security Reform', 'Social Security'),
    ('Colorado preschool Supreme Court case', 'Colorado preschool Supreme Court case'),
    ('', ''),
])
def test_archive_uses_subject_without_publisher_keyword_or_requiring_editorial_wording(topic, query):
    assert archive._archive_query(topic) == query


def test_archive_deduplicates_headlines_and_bounds_article_reads():
    data = {'news': [candidate(1, 'COPY OF - A funding report'), candidate(2),
                     candidate(3, 'Another report'), candidate(4, 'A third report'),
                     candidate(5, 'A fourth report')]}
    assert archive._archive_candidates(json.dumps(data).encode(), NOW) == [
        'https://www.ksl.com/article/1/a-funding-report',
        'https://www.ksl.com/article/3/a-funding-report',
        'https://www.ksl.com/article/4/a-funding-report']


@pytest.mark.parametrize('item', [
    candidate(url='https://ksl.com.evil.example/article/a-funding-report/123'),
    candidate(url='https://ksl.com/article/a-funding-report/456'),
    candidate(url='https://user:secret@ksl.com/article/a-funding-report/123'),
    candidate(date='Jun 9, 2024'), candidate(date='Oct 28, 2026'), candidate(date='unknown'),
])
def test_archive_metadata_never_permits_untrusted_or_mismatched_article_requests(item):
    assert archive._archive_candidates(json.dumps({'news': [item]}).encode(), NOW) == []


@pytest.mark.parametrize('author', ['Reuters', 'A. Reporter, Reuters', 'First Reporter and Second Reporter, Reuters'])
def test_named_reuters_byline_and_entire_visible_body_are_verified(author):
    assert archive._archive_reporting(page(author), URL, NOW)['excerpt'].endswith(BODY)
    assert archive._archive_reporting(page(author, visible='Only an access prompt.'), URL, NOW) is None


def test_photo_credits_and_archive_descriptions_never_become_reuters_reporting():
    assert archive._archive_reporting(page('KSL staff; photo by Reuters'), URL, NOW) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['verified', 'rejected-author', 'access-denied', 'malformed', 'oversized'])
async def test_public_archive_discovery_verifies_body_before_returning_candidates(monkeypatch, outcome):
    calls = []
    def serve(request):
        calls.append(request)
        if request.url.path == '/search/api':
            assert request.url.params['search'] == 'Social Security'
            if outcome == 'malformed':
                return httpx.Response(200, content=b'invalid', headers={'content-type': 'application/json'})
            if outcome == 'oversized':
                return httpx.Response(200, content=b'x' * 1_000_001, headers={'content-type': 'application/json'})
            return httpx.Response(200, json={'news': [candidate()]})
        assert str(request.url) == URL
        return httpx.Response(403 if outcome == 'access-denied' else 200,
            content=page('Local staff' if outcome == 'rejected-author' else 'Reuters'),
            headers={'content-type': 'text/html'})
    actual = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False
        return actual(**kwargs, transport=httpx.MockTransport(serve))
    monkeypatch.setattr(archive.httpx, 'AsyncClient', client)
    found = await archive.reuters_archive_candidates('Social Security reform: benefit cuts versus tax increases')
    assert bool(found) is (outcome == 'verified')
    assert len(calls) <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(('reply', 'accepted'), [
    ('SELECTED_STORED_CANDIDATE=0', True), ('The title looks relevant.', False),
    ('SELECTED_STORED_CANDIDATE=1', False), ('No relevant coverage found from Reuters.', False),
    ('SELECTED_STORED_CANDIDATE=0\nNo relevant coverage found from Reuters.', False),
])
async def test_verified_archive_excerpt_still_requires_full_topic_relevance_without_paid_web_search(monkeypatch, reply, accepted):
    evidence = 'Retrieved publisher excerpts (may be incomplete).\nExcerpt:\n' + BODY
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    discovery = AsyncMock(return_value=[{'url': URL, 'title': 'A funding report', 'excerpt': evidence}])
    monkeypatch.setattr(workflow, 'reuters_archive_candidates', discovery)
    paid = AsyncMock(return_value=Obj(content=[Obj(type='text', text=reply)]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    topic = 'Social Security reform: benefit cuts versus tax increases'
    _, found = await workflow.search_one_source(Mock(), topic, 'Reuters')
    assert found == (evidence if accepted else None)
    params = paid.await_args.kwargs
    assert 'tools' not in params  # Same paid stage; no unnecessary web reservation.
    assert topic in params['messages'][0]['content']
    assert 'same event/action and timing' in params['messages'][0]['content']
    paid.assert_awaited_once()
    discovery.assert_awaited_once_with(topic)


@pytest.mark.asyncio
@pytest.mark.parametrize(('scarce', 'reply', 'accepted'), [
    (True, 'SELECTED_STORED_CANDIDATE=0', True),
    (True, 'SELECTED_STORED_CANDIDATE=1', False),
    (True, 'No relevant coverage found from National Review.', False),
    (False, 'SELECTED_STORED_CANDIDATE=0', True),
])
async def test_low_headroom_can_review_verified_originals_without_promoting_metadata(monkeypatch, scarce, reply, accepted):
    monkeypatch.setattr(workflow.settings, 'ai_cost_guard_enabled', True)
    monkeypatch.setattr(workflow, 'budget_snapshot', AsyncMock(return_value={
        'enabled': True, 'daily_limit_usd': 15, 'compare_share': 0.2,
        'spent_usd': 14.4 if scarce else 1, 'held_usd': 0.1}))
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'public_feed_excerpts', AsyncMock(return_value={}))
    evidence = 'Stored publisher excerpt (may be incomplete).\nExcerpt:\n' + BODY
    monkeypatch.setattr(workflow, 'publisher_article_hints', AsyncMock(return_value=[
        {'title': 'A funding report', 'url': 'https://nationalreview.com/a-report/', 'excerpt': evidence},
        {'title': 'A different title without text', 'url': 'https://nationalreview.com/metadata-only/'}]))
    paid = AsyncMock(return_value=Obj(content=[Obj(type='text', text=reply)]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    _, found = await workflow.search_one_source(Mock(), 'Social Security funding', 'National Review')
    assert found == (evidence if accepted else None)
    params = paid.await_args.kwargs
    assert ('tools' not in params) is scarce
    if scarce:
        assert 'metadata-only' not in params['messages'][0]['content']
    paid.assert_awaited_once()
