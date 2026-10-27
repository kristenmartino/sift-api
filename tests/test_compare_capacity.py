from unittest.mock import AsyncMock, patch
import json

import pytest
from fastapi.testclient import TestClient

from services import compare_cache
from services.cost_guard import BudgetDecision
from services.paid_calls import BudgetUnavailable
from workflows import compare_workflow as workflow


def payload():
    return {'topic': 'Example event', 'comparison': 'Two outlets compared.', 'duration_ms': 50,
            'sources_checked': ['AP', 'Fox'], 'sources_requested': ['AP', 'Fox'],
            'sources_retrieved': ['AP', 'Fox'], 'sources_analyzed': ['AP', 'Fox'],
            'sources_unavailable': [], 'source_provenance_version': 1,
            'claims': [{'claim': 'The measure passed.', 'agreement': 'unanimous', 'sources': ['AP', 'Fox']}]}


def test_cache_exact_inputs_expiry_and_mutation_isolation(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(compare_cache, 'monotonic', lambda: clock[0])
    original = payload()
    compare_cache.store_comparison('Example event', ['AP', 'Fox'], original)
    original['claims'].clear()
    result = compare_cache.get_comparison(' Example event ', ['ap', 'fox'])
    assert result['claims']
    result['sources_analyzed'].clear()
    assert compare_cache.get_comparison('Example event', ['Fox', 'AP'])['claims']
    assert compare_cache.get_comparison('Example event', ['AP', 'Fox'])['sources_analyzed'] == ['AP', 'Fox']
    assert compare_cache.get_comparison('Different event', ['AP', 'Fox']) is None
    assert compare_cache.get_comparison('Example event', ['AP']) is None
    clock[0] += compare_cache.TTL_SECONDS
    assert compare_cache.get_comparison('Example event', ['AP', 'Fox']) is None


def test_cache_is_bounded_and_never_stores_placeholder_output():
    for index in range(compare_cache.MAX_ENTRIES + 1):
        compare_cache.store_comparison(str(index), ['AP', 'Fox'], payload())
    assert compare_cache.get_comparison('0', ['AP', 'Fox']) is None
    assert len(compare_cache._cache) == compare_cache.MAX_ENTRIES
    compare_cache.store_comparison('empty', ['AP'], {**payload(), 'claims': []})
    assert compare_cache.get_comparison('empty', ['AP']) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['AI_BUDGET_EXCEEDED', 'COST_GUARD_UNAVAILABLE'])
async def test_one_denied_search_does_not_discard_other_verified_reporting(monkeypatch, code):
    monkeypatch.setattr(workflow, 'load_allowed_sources', AsyncMock(return_value={'ap', 'fox', 'reuters'}))
    monkeypatch.setattr(workflow, 'compare_search_parallelism', AsyncMock(return_value=1))
    async def search(_client, _topic, source):
        if source == 'Reuters':
            raise BudgetUnavailable('No room for search', code)
        return source, source + ' publisher reporting'
    monkeypatch.setattr(workflow, 'search_one_source', search)
    if code == 'COST_GUARD_UNAVAILABLE':
        with pytest.raises(BudgetUnavailable) as error:
            await workflow.search_sources_node({'topic': 'Example event', 'sources': ['AP', 'Reuters', 'Fox']})
        assert error.value.code == code
        return
    result = await workflow.search_sources_node({'topic': 'Example event', 'sources': ['AP', 'Reuters', 'Fox']})
    assert result['search_results'] == {'AP': 'AP publisher reporting', 'Fox': 'Fox publisher reporting'}
    assert result['capacity_limited'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['AI_BUDGET_EXCEEDED', 'COST_GUARD_UNAVAILABLE'])
async def test_no_usable_reporting_preserves_the_budget_failure(monkeypatch, code):
    monkeypatch.setattr(workflow, 'load_allowed_sources', AsyncMock(return_value={'ap'}))
    monkeypatch.setattr(workflow, 'compare_search_parallelism', AsyncMock(return_value=1))
    monkeypatch.setattr(workflow, 'search_one_source', AsyncMock(side_effect=BudgetUnavailable('blocked', code)))
    with pytest.raises(BudgetUnavailable) as error:
        await workflow.search_sources_node({'topic': 'Example event', 'sources': ['AP']})
    assert error.value.code == code


@pytest.fixture
def client():
    with patch('app.db.init_pool', new_callable=AsyncMock):
        from app.main import app
        with patch('app.routers.compare.limiter.enabled', False), TestClient(app) as client:
            yield client


@pytest.mark.parametrize('stream', [False, True])
def test_completed_result_reuse_does_no_new_paid_work_even_at_cap(client, stream):
    compare_cache.store_comparison('Example event', ['AP', 'Fox'], payload())
    with patch('app.routers.compare.check_budget', AsyncMock(side_effect=AssertionError('No new spend needed'))), patch(
        'app.routers.compare.compare_graph.ainvoke', AsyncMock(side_effect=AssertionError('No new generation')),
    ), patch('app.routers.compare.search_one_source', AsyncMock(side_effect=AssertionError('No new search'))):
        response = client.post('/analyze/compare' + ('/stream' if stream else ''),
            json={'topic': 'Example event', 'sources': ['AP', 'Fox']}, headers={'X-Pipeline-Key': 'dev-key'})
    assert response.status_code == 200
    result = json.loads(response.text.split('data: ', 1)[1]) if stream else response.json()
    assert result['sources_analyzed'] == ['AP', 'Fox']
    assert result['claims'][0]['sources'] == ['AP', 'Fox']


def test_cached_result_still_requires_authentication(client):
    compare_cache.store_comparison('Example event', ['AP', 'Fox'], payload())
    response = client.post('/analyze/compare/stream', json={'topic': 'Example event', 'sources': ['AP', 'Fox']},
                           headers={'X-Pipeline-Key': 'wrong'})
    assert response.status_code == 401


def test_stream_finishes_available_sources_after_another_search_is_denied(client):
    async def search(_client, _topic, source):
        if source == 'Reuters':
            raise BudgetUnavailable('No room for search')
        return source, source + ' reporting'
    extracted = {'comparison': 'Two outlets compared.', 'analyzed_sources': ['AP', 'Fox'], 'claims': payload()['claims']}
    with patch('app.routers.compare.check_budget', AsyncMock(return_value=BudgetDecision(True, 'ok', 13, 15))), patch(
        'app.routers.compare.load_allowed_sources', AsyncMock(return_value={'ap', 'fox', 'reuters'}),
    ), patch('app.routers.compare.compare_search_parallelism', AsyncMock(return_value=1)), patch(
        'app.routers.compare.search_one_source', side_effect=search,
    ), patch('app.routers.compare.extract_and_compare_node', AsyncMock(return_value=extracted)):
        response = client.post('/analyze/compare/stream', json={'topic': 'Example event', 'sources': ['AP', 'Reuters', 'Fox']},
                               headers={'X-Pipeline-Key': 'dev-key'})
    frame = next(f for f in response.text.split('\n\n') if f.startswith('event: results'))
    result = json.loads(frame.split('data: ', 1)[1])
    assert result['sources_analyzed'] == ['AP', 'Fox']
    assert result['sources_unavailable'] == ['Reuters']
    assert result['claims'][0]['sources'] == ['AP', 'Fox']
    assert compare_cache.get_comparison('Example event', ['AP', 'Reuters', 'Fox']) is None


@pytest.mark.parametrize('first_stream', [False, True])
def test_fresh_success_is_reused_across_both_endpoint_formats(client, first_stream):
    state = {**payload(), 'sources': ['AP', 'Fox'], 'analyzed_sources': ['AP', 'Fox'],
             'search_results': {'AP': 'AP reporting', 'Fox': 'Fox reporting'}, 'errors': []}
    graph = AsyncMock(return_value=state)
    search = AsyncMock(side_effect=lambda _client, _topic, source: (source, source + ' reporting'))
    budget = AsyncMock(return_value=BudgetDecision(True, 'ok', 13, 15))
    with patch('app.routers.compare.check_budget', budget), patch('app.routers.compare.compare_graph.ainvoke', graph), patch(
        'app.routers.compare.load_allowed_sources', AsyncMock(return_value={'ap', 'fox'}),
    ), patch('app.routers.compare.compare_search_parallelism', AsyncMock(return_value=1)), patch(
        'app.routers.compare.search_one_source', search,
    ), patch('app.routers.compare.extract_and_compare_node', AsyncMock(return_value=state)):
        first = client.post('/analyze/compare' + ('/stream' if first_stream else ''),
            json={'topic': 'Example event', 'sources': ['AP', 'Fox']}, headers={'X-Pipeline-Key': 'dev-key'})
        assert first.status_code == 200
        budget.side_effect = AssertionError('Cached read must not need new capacity')
        second = client.post('/analyze/compare' + ('' if first_stream else '/stream'),
            json={'topic': 'Example event', 'sources': ['Fox', 'AP']}, headers={'X-Pipeline-Key': 'dev-key'})
        assert second.status_code == 200
        budget.assert_awaited_once()
        assert graph.await_count == (0 if first_stream else 1)
        assert search.await_count == (2 if first_stream else 0)
