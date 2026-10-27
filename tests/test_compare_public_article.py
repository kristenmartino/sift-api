import json
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest

from services.compare_public_article import article_excerpt, public_article_excerpt, public_article_candidates
from workflows import compare_workflow as workflow

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
URL = 'https://www.wired.com/story/a-privacy-report/'
BODY = ('The researchers tested fifty public privacy request forms across several companies. '
        'They found that several forms did not allow users to finish submitting requests. '
        'The authors said their findings concern the tested forms rather than every company. '
        'They recommended that each company test its public removal process again.')


def page(body=BODY, visible=None, **extra):
    schema = {'@type': 'NewsArticle', 'url': URL, 'author': {'name': 'A Reporter'},
              'datePublished': '2026-05-20T09:00:00Z', 'articleBody': body, **extra}
    return f'<script type="application/ld+json">{json.dumps(schema)}</script><article>{visible if visible is not None else "<p>" + body + "</p>"}</article>'


def test_public_article_requires_visible_original_reporting():
    result = article_excerpt(page(isAccessibleForFree=True), URL, 'WIRED', NOW)
    assert BODY.split('. ')[0] in result
    assert 'Published: 2026-05-20' in result
    assert article_excerpt(page(visible='<h1>A privacy report</h1>'), URL, 'wired', NOW) is None


@pytest.mark.parametrize('attrs', ['hidden', 'aria-hidden="true"', 'style="display: none"', 'style="visibility:hidden"'])
def test_hidden_schema_text_cannot_be_used_to_bypass_access(attrs):
    assert article_excerpt(page(visible=f'<div {attrs}>{BODY}</div>'), URL, 'wired', NOW) is None


@pytest.mark.parametrize('extra', [
    {'isAccessibleForFree': False}, {'isAccessibleForFree': 'false'},
    {'hasPart': {'isAccessibleForFree': False}}, {'url': URL + 'wrong/'},
    {'datePublished': '2024-01-01T00:00:00Z'}, {'datePublished': '2027-01-01T00:00:00Z'},
    {'datePublished': '2026-05-20'}, {'author': None},
])
def test_gated_unattributed_stale_future_and_other_article_schema_rejected(extra):
    assert article_excerpt(page(**extra), URL, 'wired', NOW) is None


@pytest.mark.asyncio
async def test_unknown_or_host_spoofed_url_never_triggers_http(monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr('services.compare_public_article._get', get)
    assert await public_article_excerpt('https://wired.com.evil.example/story/a/', 'wired') is None
    assert await public_article_excerpt('https://www.wired.com/story/a/', 'Other outlet') is None
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_discovery_preserves_verified_candidate_when_second_read_fails(monkeypatch):
    from services import compare_public_article as public
    get = AsyncMock(side_effect=['/story/a-privacy-report/ /story/a-second-report/', page(), TimeoutError()])
    monkeypatch.setattr(public, '_get', get)
    candidates = await public_article_candidates("Data brokers don't just collect facts. They guess about you", 'wired')
    assert len(candidates) == 1
    assert get.await_args_list[0].args[1] == 'https://www.wired.com/search/?q=Data%20brokers'


@pytest.mark.asyncio
async def test_exact_indexed_original_avoids_paid_search(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value={
        'title': 'A privacy report', 'url': URL, 'excerpt': None}))
    monkeypatch.setattr(workflow, 'public_article_excerpt', AsyncMock(return_value='Verified original'))
    paid = AsyncMock()
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    assert await workflow.search_one_source(Mock(), 'A privacy report', 'wired') == ('wired', 'Verified original')
    paid.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('reply', 'accepted'), [('SELECTED_STORED_CANDIDATE=0', True),
    ('No relevant coverage found from wired.', False), ('This looks related.', False)])
async def test_visible_candidate_still_requires_full_topic_relevance_selection(monkeypatch, reply, accepted):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    original = article_excerpt(page(), URL, 'wired', NOW)
    monkeypatch.setattr(workflow, 'public_article_candidates', AsyncMock(return_value=[
        {'title': 'A privacy report', 'url': URL, 'excerpt': original}]))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    paid = AsyncMock(return_value=Obj(content=[Obj(type='text', text=reply)]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    _, text = await workflow.search_one_source(Mock(), 'A privacy request investigation', 'wired')
    assert text == (original if accepted else None)
    assert 'tools' not in paid.await_args.kwargs
    assert paid.await_args.kwargs['max_tokens'] == 128
