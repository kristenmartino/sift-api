from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest

from services.compare_search import publisher_domains, publisher_search_excerpts
from workflows import compare_workflow as workflow


PASSAGE = 'The committee approved the measure by ten votes to two after a public hearing on Monday.'


def response(quote=PASSAGE, url='https://www.nationalreview.com/2026/10/a-vote/', title='A vote'):
    return Obj(content=[Obj(type='text', text='The committee approved the measure.', citations=[
        Obj(type='web_search_result_location', cited_text=quote, url=url, title=title),
    ])])


@pytest.mark.parametrize('name', ['National Review', 'national-review', ' NATIONAL REVIEW '])
def test_national_review_name_and_slug_use_the_publisher_domain(name):
    assert publisher_domains(name) == ('nationalreview.com',)


@pytest.mark.asyncio
async def test_reuters_preserves_compatible_search_without_rejected_domain_constraint(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    legacy = Obj(content=[Obj(type='text', text='Reuters reports that the committee approved the measure after a public hearing.')])
    paid = AsyncMock(return_value=legacy)
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    source, text = await workflow.search_one_source(Mock(), 'The committee vote', 'Reuters')
    assert source == 'Reuters'
    assert text == legacy.content[0].text
    assert 'allowed_domains' not in paid.await_args.kwargs['tools'][0]
    paid.assert_awaited_once()


def test_public_search_passage_is_evidence_even_when_direct_page_is_blocked():
    evidence = publisher_search_excerpts(response(), ('nationalreview.com',))
    assert PASSAGE in evidence
    assert 'may be incomplete' in evidence
    assert 'The committee approved the measure.\n' not in evidence


@pytest.mark.parametrize('url', ['https://nationalreview.com.evil.example/article',
                                 'https://another-outlet.example/article', 'http://www.nationalreview.com/article'])
def test_other_hosts_and_insecure_urls_never_supply_selected_outlet_evidence(url):
    assert publisher_search_excerpts(response(url=url), ('nationalreview.com',)) is None


@pytest.mark.parametrize(('quote', 'title'), [
    ('Subscribe to read the full article and get unlimited access to our reporting.', 'A vote'),
    ('\n Subscribe to read the full article and get unlimited access to our reporting.', 'A vote'),
    ('The committee approved the measure by ten votes to two', 'The committee approved the measure by ten votes to two'),
    ('Read more', 'A vote'),
])
def test_access_prompts_headlines_and_thin_metadata_do_not_count(quote, title):
    assert publisher_search_excerpts(response(quote=quote, title=title), ('nationalreview.com',)) is None


def test_model_text_without_provider_citations_is_not_publisher_reporting():
    uncited = Obj(content=[Obj(type='text', text=PASSAGE)])
    assert publisher_search_excerpts(uncited, ('nationalreview.com',)) is None


def test_known_article_does_not_substitute_neighboring_publisher_search_results():
    assert publisher_search_excerpts(response(), ('nationalreview.com',),
                                     'https://www.nationalreview.com/2026/10/different-event/') is None
    assert publisher_search_excerpts(response(url='https://nationalreview.com/2026/10/a-vote/amp/'),
                                     ('nationalreview.com',),
                                     'https://www.nationalreview.com/2026/10/a-vote/')


def test_article_identity_preserves_content_query_ids_and_ignores_tracking():
    assert publisher_search_excerpts(response(url='https://nationalreview.com/?p=2'),
                                     ('nationalreview.com',), 'https://nationalreview.com/?p=1') is None
    assert publisher_search_excerpts(response(), ('nationalreview.com',),
                                     'https://nationalreview.com/2026/10/a-vote/?utm_source=rss')


@pytest.mark.asyncio
async def test_known_national_review_article_is_targeted_with_one_scoped_search(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value={
        'excerpt': None, 'title': 'A committee vote', 'url': 'https://www.nationalreview.com/2026/10/a-vote/',
    }))
    paid = AsyncMock(return_value=response())
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    source, evidence = await workflow.search_one_source(Mock(), 'A rewritten Sift headline', 'National Review')
    assert source == 'National Review'
    assert PASSAGE in evidence
    params = paid.await_args.kwargs
    assert params['tools'][0]['allowed_domains'] == ['nationalreview.com']
    assert params['tools'][0]['max_uses'] == 1
    assert 'site:nationalreview.com A committee vote' in params['messages'][0]['content']
    paid.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_no_coverage_does_not_promote_unrelated_search_citations(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    no_coverage = response()
    no_coverage.content[0].text = 'No relevant coverage found from National Review.'
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=no_coverage))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    assert await workflow.search_one_source(Mock(), 'An unrelated topic', 'National Review') == ('National Review', None)


@pytest.mark.asyncio
async def test_broad_topic_uses_publisher_title_hints_without_counting_metadata(monkeypatch):
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'publisher_article_hints', AsyncMock(return_value=[
        {'title': 'A committee vote explained', 'url': 'https://www.nationalreview.com/2026/10/a-vote/'},
    ]))
    paid = AsyncMock(return_value=response())
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    _, evidence = await workflow.search_one_source(Mock(), 'Public policy committee decision', 'National Review')
    assert PASSAGE in evidence
    assert 'site:nationalreview.com A committee vote explained' in paid.await_args.kwargs['messages'][0]['content']
    paid.return_value = Obj(content=[Obj(type='text', text='A committee vote explained')])
    assert await workflow.search_one_source(Mock(), 'Public policy committee decision', 'National Review') == ('National Review', None)


@pytest.mark.asyncio
@pytest.mark.parametrize(('reply', 'has_excerpt', 'accepted'), [
    ('SELECTED_STORED_CANDIDATE=0', True, True),
    ('A title looks relevant.', True, False),
    ('SELECTED_STORED_CANDIDATE=2', True, False),
    ('SELECTED_STORED_CANDIDATE=0', False, False),
    ('SELECTED_STORED_CANDIDATE=0\nNo relevant coverage found from National Review.', True, False),
])
async def test_stored_discovery_excerpt_requires_explicit_relevance_selection(monkeypatch, reply, has_excerpt, accepted):
    original = 'Stored publisher excerpt (may be incomplete).\nExcerpt:\n' + PASSAGE
    hint = {'title': 'A committee vote explained', 'url': 'https://www.nationalreview.com/a-vote'}
    if has_excerpt:
        hint['excerpt'] = original
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'publisher_article_hints', AsyncMock(return_value=[hint]))
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=Obj(content=[Obj(type='text', text=reply)])))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    _, evidence = await workflow.search_one_source(Mock(), 'Public policy committee decision', 'National Review')
    assert evidence == (original if accepted else None)
