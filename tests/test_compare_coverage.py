from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from services import compare_coverage
from workflows import compare_workflow


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [None, 'A short teaser.'])
async def test_indexed_metadata_guides_retrieval_but_does_not_count_as_reporting(monkeypatch, body):
    row = {'title': 'A committee voted', 'source_url': 'https://example.com/vote',
           'published_date': datetime.now(timezone.utc), 'generation_raw_content': body}
    pool = Mock(fetchrow=AsyncMock(return_value=row))
    monkeypatch.setattr(compare_coverage, 'get_pool', AsyncMock(return_value=pool))
    result = await compare_coverage.indexed_coverage('A committee voted', 'Reuters')
    assert result == {'excerpt': None, 'title': row['title'], 'url': row['source_url']}


@pytest.mark.asyncio
async def test_preserves_publisher_text_and_url_without_generated_summary(monkeypatch):
    row = {'title': 'A committee voted', 'source_url': 'https://example.com/vote',
           'published_date': datetime.now(timezone.utc), 'generation_raw_content': '<p>Original publisher reporting &amp; evidence. </p>' * 10}
    monkeypatch.setattr(compare_coverage, 'get_pool', AsyncMock(return_value=Mock(fetchrow=AsyncMock(return_value=row))))
    result = await compare_coverage.indexed_coverage('A committee voted', 'Reuters')
    assert result['excerpt'].count('Original publisher reporting & evidence.') == 10
    assert 'https://example.com/vote' in result['excerpt']
    assert '<p>' not in result['excerpt']


@pytest.mark.asyncio
async def test_index_miss_or_database_failure_falls_back_without_fabricating_coverage(monkeypatch):
    pool = Mock(fetchrow=AsyncMock(return_value=None))
    monkeypatch.setattr(compare_coverage, 'get_pool', AsyncMock(return_value=pool))
    assert await compare_coverage.indexed_coverage('Unrelated event', 'Reuters') is None
    pool.fetchrow.side_effect = TimeoutError()
    assert await compare_coverage.indexed_coverage('Unrelated event', 'Reuters') is None


@pytest.mark.asyncio
async def test_stored_reporting_avoids_a_paid_search(monkeypatch):
    monkeypatch.setattr(compare_workflow, 'indexed_coverage', AsyncMock(return_value={'excerpt': 'Original reporting.', 'title': 'A vote', 'url': 'https://example.com/a'}))
    paid = AsyncMock(side_effect=AssertionError('paid search must not run'))
    monkeypatch.setattr(compare_workflow, 'budgeted_message', paid)
    assert await compare_workflow.search_one_source(Mock(), 'A vote', 'Reuters') == ('Reuters', 'Original reporting.')
    paid.assert_not_awaited()


@pytest.mark.asyncio
async def test_indexed_url_is_only_a_hint_until_live_search_returns_reporting(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(compare_workflow, 'indexed_coverage', AsyncMock(return_value={'excerpt': None, 'title': 'A vote', 'url': 'https://example.com/vote'}))
    paid = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type='text', text='No relevant coverage found from Reuters.')]))
    monkeypatch.setattr(compare_workflow, 'budgeted_message', paid)
    monkeypatch.setattr(compare_workflow, 'log_usage', Mock())
    _, text = await compare_workflow.search_one_source(Mock(), 'A vote', 'Reuters')
    assert not compare_workflow.source_found_coverage(text)
    params = paid.await_args.kwargs
    assert params['stream_response'] is True
    assert 'https://example.com/vote' in params['messages'][0]['content']
