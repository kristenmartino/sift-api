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
    assert get.await_args_list[0].args[1] == 'https://www.wired.com/tag/data-brokers/'


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
    if accepted:
        reply += '\nSUPPORTING_PASSAGE=“' + BODY.split('. ')[0] + '.”'
    paid = AsyncMock(return_value=Obj(content=[Obj(type='text', text=reply)]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    _, text = await workflow.search_one_source(Mock(), 'A privacy request investigation', 'wired')
    assert text == (original if accepted else None)
    assert 'tools' not in paid.await_args.kwargs
    assert paid.await_args.kwargs['max_tokens'] == 1024


def test_abbreviations_and_inline_markup_keep_complete_visible_paragraphs():
    body = ('The U.S. committee approved a measure after reviewing the public report. '
            'A spokesperson said the decision applies only to requests made this year. '
            'The agency plans to publish an assessment after the next quarterly review. '
            'Officials said no other decision was announced during the meeting.')
    visible = '<p>' + body.replace('committee', '<a href="/committee">committee</a>') + '</p>'
    result = article_excerpt(page(body=body, visible=visible), URL, 'wired', NOW)
    assert result.partition('\nExcerpt:\n')[2] == body


def test_schema_and_visible_partial_paragraphs_are_not_stitched_into_reporting():
    assert article_excerpt(page(visible='<p>' + BODY[:85] + '</p><p>' + BODY[-70:] + '</p>'), URL, 'wired', NOW) is None


@pytest.mark.asyncio
async def test_unfinished_relevance_output_cannot_select_a_publisher(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'public_article_candidates', AsyncMock(return_value=[
        {'title': 'A privacy report', 'url': URL, 'excerpt': BODY}]))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=Obj(
        stop_reason='max_tokens', content=[Obj(type='text', text='SELECTED_STORED_CANDIDATE=0')])))
    assert await workflow.search_one_source(Mock(), 'A privacy request investigation', 'wired') == ('wired', None)


@pytest.mark.asyncio
async def test_other_subjects_keep_generic_publisher_discovery(monkeypatch):
    get = AsyncMock(return_value='No published story links')
    monkeypatch.setattr('services.compare_public_article._get', get)
    assert await public_article_candidates('Phone repairability review', 'wired') == []
    assert get.await_args.args[1] == 'https://www.wired.com/search/?q=Phone%20repairability%20review'


@pytest.mark.asyncio
async def test_case_study_selection_requires_an_original_supporting_passage(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'public_article_candidates', AsyncMock(return_value=[
        {'title': 'A privacy report', 'url': URL, 'excerpt': article_excerpt(page(), URL, 'wired', NOW)}]))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    paid = AsyncMock(return_value=Obj(content=[Obj(type='text',
        text='SELECTED_STORED_CANDIDATE=0\nSUPPORTING_PASSAGE=The company inferred sensitive medical conditions from incomplete records about individuals.')] ))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    assert await workflow.search_one_source(Mock(), 'Companies infer sensitive medical conditions', 'wired') == ('wired', None)
    prompt = paid.await_args.kwargs['messages'][0]['content']
    assert 'concrete case study' in prompt
    assert 'same event/action and timing' in prompt


@pytest.mark.asyncio
async def test_long_contiguous_relevance_quote_does_not_discard_a_valid_case(monkeypatch):
    long_body = BODY + ' ' + BODY
    assert 60 < len(long_body.split()) < 300
    original = article_excerpt(page(body=long_body), URL, 'wired', NOW)
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'public_article_candidates', AsyncMock(return_value=[
        {'title': 'A privacy report', 'url': URL, 'excerpt': original}]))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=Obj(content=[
        Obj(type='text', text='SELECTED_STORED_CANDIDATE=0\nSUPPORTING_PASSAGE=' + long_body)])))
    assert await workflow.search_one_source(Mock(), 'Public privacy request processes', 'wired') == ('wired', original)
