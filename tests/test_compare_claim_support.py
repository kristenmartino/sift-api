import json
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest

from services import compare_claim_support as support
from services.paid_calls import BudgetUnavailable
from workflows import compare_workflow as workflow

FOX = 'Health information inferred by businesses outside healthcare may fall outside HIPAA protections.'
WIRED = 'The service generates an AI overview and flags potential problems in personal background reports.'


def claims():
    return [{'claim': 'Data broker health profiles may fall outside HIPAA protections.',
             'agreement': 'unanimous', 'sources': ['Fox News', 'WIRED'],
             'evidence': {'Fox News': FOX, 'WIRED': WIRED}},
            {'claim': 'The service generates AI overviews of personal background reports.',
             'agreement': 'unique', 'sources': ['WIRED'], 'evidence': {'WIRED': WIRED}}]


def reply(value, **extra):
    return Obj(content=[Obj(type='text', text=json.dumps(value))], **extra)


CNBC_CLAUSE = ('The software maker announced a new voluntary exit offer for certain workers '
               'in the U.S., Canada, India and the United Kingdom')
CNBC_PREVIEW = ('Retrieved publisher excerpts (may be incomplete).\n'
                'Original: https://www.cnbc.com/2026/10/26/microsoft-msft-voluntary-buyouts.html\n'
                'Excerpt:\n' + CNBC_CLAUSE + ' at its experience level ...')


@pytest.mark.asyncio
async def test_missing_outlet_can_recover_only_its_own_intact_clause(monkeypatch):
    async def respond(*args, **kwargs):
        prompt = kwargs['messages'][0]['content']
        assert CNBC_CLAUSE in prompt
        assert '60000' not in prompt
        assert WIRED not in prompt
        assert 'certain workers' in prompt
        assert json.loads(prompt.split('Pairs:\n')[1]) == []
        return reply({'supported': [], 'outlet_passage': CNBC_CLAUSE})
    paid = AsyncMock(side_effect=respond)
    monkeypatch.setattr(support, 'budgeted_message', paid)
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), [], {'CNBC': CNBC_PREVIEW},
        topic='Microsoft voluntary exit offer', repair_missing=True)
    assert not limited
    assert result[0]['claim'] == f'CNBC writes: “{CNBC_CLAUSE}”'
    assert result[0]['sources'] == ['CNBC']
    assert '60,000' not in result[0]['claim']
    assert 'level 70' not in result[0]['claim']
    assert paid.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('passage', [
    CNBC_CLAUSE + '.', CNBC_CLAUSE.replace('certain workers', '60,000 workers'),
    CNBC_CLAUSE + ' at its experience level', CNBC_CLAUSE + ' at its experience level ...',
    CNBC_CLAUSE.removeprefix('The software maker '),
    'Microsoft offers voluntary buyouts to 60,000 employees.', True,
])
async def test_repair_rejects_completed_fragments_changed_words_and_borrowed_claims(monkeypatch, passage):
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply({
        'supported': [], 'outlet_passage': passage})))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), [], {'CNBC': CNBC_PREVIEW},
        topic='Microsoft voluntary exit offer', repair_missing=True)
    assert result == []
    assert limited == {'CNBC'}


@pytest.mark.asyncio
async def test_null_repair_is_successful_review_without_manufacturing_a_claim(monkeypatch):
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply({
        'supported': [], 'outlet_passage': None})))
    monkeypatch.setattr(support, 'log_usage', Mock())
    assert await support.verify_claim_support(Mock(), [], {'CNBC': CNBC_PREVIEW},
        topic='Microsoft voluntary exit offer', repair_missing=True) == ([], set())


@pytest.mark.asyncio
async def test_successfully_rejected_pair_cannot_be_reintroduced_as_a_repair(monkeypatch):
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply({
        'supported': [], 'outlet_passage': WIRED})))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), [claims()[1]], {'WIRED': WIRED},
        topic='AI personal profiling', repair_missing=True)
    assert result == []
    assert not limited


@pytest.mark.asyncio
async def test_null_first_extraction_still_runs_isolated_repair_and_context_guards(monkeypatch):
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=reply({
        'claims': [], 'outlet_passages': {'CNBC': None}})))
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply({
        'supported': [], 'outlet_passage': CNBC_CLAUSE})))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(support, 'log_usage', Mock())
    result = await workflow.extract_and_compare_node({'topic': 'Microsoft voluntary exit offer',
        'sources': ['CNBC'], 'search_results': {'CNBC': CNBC_PREVIEW}})
    assert result['claims'][0]['claim'] == f'CNBC writes: “{CNBC_CLAUSE}”'
    assert '_quote_repair' not in result['claims'][0]
    assert not result['capacity_limited']


def test_repair_cannot_treat_abbreviation_as_sentence_boundary():
    quote = 'announced a voluntary exit offer for certain workers'
    assert not support._repair_clause_from_original(quote, 'The company in the U.S. ' + quote + '.')
    assert support._repair_clause_from_original('The company ' + quote,
        'An earlier report was unrelated. The company ' + quote + ' at level ...')


@pytest.mark.asyncio
async def test_review_isolates_publishers_and_removes_wrong_law_without_rewriting_claims(monkeypatch):
    async def respond(*args, **kwargs):
        prompt = kwargs['messages'][0]['content']
        pairs = json.loads(prompt.split('Pairs:\n')[1])
        if pairs[0]['source'] == 'WIRED':
            assert FOX not in prompt
            return reply({'supported': [2]})
        assert WIRED not in prompt
        return reply({'supported': [0]})
    paid = AsyncMock(side_effect=respond)
    monkeypatch.setattr(support, 'budgeted_message', paid)
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), claims(), {'Fox News': FOX, 'WIRED': WIRED}, topic='Personal data profiling')
    assert not limited
    assert result[0]['sources'] == ['Fox News']
    assert result[1]['sources'] == ['WIRED']
    assert result[0]['claim'] == claims()[0]['claim']
    assert result[1]['claim'] == claims()[1]['claim']
    assert result[0]['evidence'] == {'Fox News': FOX}
    assert paid.await_count == 2
    assert all(call.args[1] == 'compare.verify_claim_support' for call in paid.await_args_list)
    assert all(call.kwargs['max_tokens'] == 512 and not call.kwargs.get('tools') for call in paid.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [{}, {'supported': 'all'}, {'supported': [-1]},
    {'supported': [999]}, {'supported': [True]}, {'supported': ['0']}])
async def test_malformed_review_cannot_authorize_claim_attribution(monkeypatch, value):
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply(value)))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), claims(), {'Fox News': FOX, 'WIRED': WIRED}, topic='Personal data profiling')
    assert result == []
    assert limited


@pytest.mark.asyncio
async def test_truncated_review_cannot_accept_ids(monkeypatch):
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply({'supported': [0]}, stop_reason='max_tokens')))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), claims(), {'Fox News': FOX, 'WIRED': WIRED}, topic='Personal data profiling')
    assert result == []
    assert limited


@pytest.mark.asyncio
async def test_dispute_needs_independently_supported_both_sides(monkeypatch):
    candidate = {'claim': 'The measure passed.', 'agreement': 'disputed', 'sources_for': ['A'],
                 'sources_against': ['B'], 'evidence': {'A': FOX, 'B': WIRED}}
    async def respond(*args, **kwargs):
        pairs = json.loads(kwargs['messages'][0]['content'].split('Pairs:\n')[1])
        return reply({'supported': [0] if pairs[0]['source'] == 'A' else []})
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(side_effect=respond))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), [candidate], {'A': FOX, 'B': WIRED}, topic='The committee vote')
    assert result == []
    assert not limited


@pytest.mark.parametrize('law', ['HIPAA', 'Health Insurance Portability and Accountability Act'])
def test_named_law_absent_from_outlet_cannot_be_inherited_from_another_outlet(law):
    candidate = claims()[0]
    candidate['claim'] = f'Health profiles fall outside {law} protections.'
    result = workflow._ground_claim_attributions([candidate], {'Fox News': FOX, 'WIRED': WIRED}, include_evidence=True)
    assert result[0]['sources'] == ['Fox News']
    assert result[0]['evidence'] == {'Fox News': FOX}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [ValueError('invalid review'), BudgetUnavailable('cap')])
async def test_unavailable_review_returns_only_checked_literal_quotes(monkeypatch, failure):
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=reply({
        'claims': [claims()[1]], 'outlet_passages': {'WIRED': WIRED}})))
    monkeypatch.setattr(workflow, 'verify_claim_support', AsyncMock(side_effect=failure))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    result = await workflow.extract_and_compare_node({'topic': 'AI personal profiling', 'sources': ['WIRED'],
                                                       'search_results': {'WIRED': WIRED}})
    assert result['claims'][0]['claim'] == f'WIRED writes: “{WIRED}”'
    assert result['capacity_limited'] is True


@pytest.mark.asyncio
async def test_one_denied_review_keeps_other_verified_outlet_claims(monkeypatch):
    async def respond(*args, **kwargs):
        pairs = json.loads(kwargs['messages'][0]['content'].split('Pairs:\n')[1])
        if pairs[0]['source'] == 'WIRED':
            raise BudgetUnavailable('cap')
        return reply({'supported': [0]})
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(side_effect=respond))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), claims(), {'Fox News': FOX, 'WIRED': WIRED}, topic='Personal data profiling')
    assert result[0]['sources'] == ['Fox News']
    assert limited


@pytest.mark.asyncio
async def test_accounting_failure_still_fails_closed(monkeypatch):
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=reply({'claims': [claims()[1]]})))
    monkeypatch.setattr(workflow, 'verify_claim_support', AsyncMock(side_effect=BudgetUnavailable('accounting', 'COST_GUARD_UNAVAILABLE')))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    with pytest.raises(BudgetUnavailable, match='accounting'):
        await workflow.extract_and_compare_node({'topic': 'AI personal profiling', 'sources': ['WIRED'],
                                                 'search_results': {'WIRED': WIRED}})


@pytest.mark.asyncio
async def test_successfully_rejected_literal_backup_cannot_reenter_after_review(monkeypatch):
    unrelated = 'The city council approved the parking schedule after a public hearing.'
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=reply({
        'claims': [], 'outlet_passages': {'WIRED': unrelated}})))
    verifier = AsyncMock(return_value=([], set()))
    monkeypatch.setattr(workflow, 'verify_claim_support', verifier)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    result = await workflow.extract_and_compare_node({'topic': 'AI personal profiling', 'sources': ['WIRED'],
                                                       'search_results': {'WIRED': unrelated}})
    assert result['claims'] == []
    assert verifier.await_args.kwargs['topic'] == 'AI personal profiling'
    assert result['capacity_limited'] is False


@pytest.mark.parametrize('verb', ['offered', 'promised'])
def test_reporting_ongoing_work_does_not_establish_an_offer_or_promise(verb):
    cbs = 'The director said the intelligence community is working to downgrade sensitive material.'
    axios = f'The director {verb} to downgrade sensitive material for personnel at risk.'
    candidate = {'claim': f'The director {verb} to downgrade sensitive material.',
                 'sources': ['CBS', 'Axios'], 'evidence': {'CBS': cbs, 'Axios': axios}}
    result = workflow._ground_claim_attributions([candidate], {'CBS': cbs, 'Axios': axios})
    assert result[0]['sources'] == ['Axios']


def test_explicit_willingness_can_support_an_offer_without_literal_same_verb():
    text = 'The director said he was willing to share downgraded material with personnel at risk.'
    candidate = {'claim': 'The director offered to share downgraded material.', 'sources': ['CBS'],
                 'evidence': {'CBS': text}}
    assert workflow._ground_claim_attributions([candidate], {'CBS': text})[0]['sources'] == ['CBS']
