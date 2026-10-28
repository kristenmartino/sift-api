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


@pytest.mark.parametrize(('body', 'selected', 'restored'), [
    (CNBC_CLAUSE + ' at its experience level ...', CNBC_CLAUSE.removeprefix('The software maker '), CNBC_CLAUSE),
    ('In its latest gift to oil and gas companies, the Trump administration is expected to announce plans today to weaken rules that guard against methane leaks.',
     'the Trump administration is expected to announce plans today to weaken rules that guard against methane leaks',
     'In its latest gift to oil and gas companies, the Trump administration is expected to announce plans today to weaken rules that guard against methane leaks'),
    ('But a ruling last week by a New Mexico judge blew that number out of the water.',
     'a New Mexico judge blew that number out of the water',
     'But a ruling last week by a New Mexico judge blew that number out of the water'),
    ('If approved, the company could offer voluntary exits to certain workers at level ...',
     'the company could offer voluntary exits to certain workers',
     'If approved, the company could offer voluntary exits to certain workers'),
    ('An earlier result was final. The agency said the company may offer voluntary exits next month.',
     'the company may offer voluntary exits next month',
     'The agency said the company may offer voluntary exits next month'),
    ('Microsoft Corp. announced voluntary exits for certain workers at level ...',
     'announced voluntary exits for certain workers',
     'Microsoft Corp. announced voluntary exits for certain workers'),
])
def test_dropped_opening_is_restored_only_from_contiguous_original(body, selected, restored):
    assert support._restore_repair_context(selected, body) == restored


def test_restoration_never_fills_missing_words_or_changes_numbers():
    assert support._restore_repair_context(CNBC_CLAUSE + '.', CNBC_PREVIEW.partition('Excerpt:\n')[2]) is None
    assert support._restore_repair_context(CNBC_CLAUSE.replace('certain', '60000'), CNBC_PREVIEW) is None


@pytest.mark.asyncio
async def test_relative_day_quote_survives_as_separately_reviewed_backup_when_calendar_claim_fails(monkeypatch):
    quote = "Jio Platforms' shares rose 21.4% on their trading debut Wednesday"
    original = 'Retrieved publisher excerpts.\nExcerpt:\n' + quote + ' after a record IPO that was lapped up by retail as well as insti...'
    monkeypatch.setattr(workflow, 'budgeted_message', AsyncMock(return_value=reply({
        'outlet_passages': {'CNBC': original.partition('Excerpt:\n')[2]},
        'claims': [{'claim': "Jio Platforms' shares rose 21.4% on their trading debut in October 2026.",
                    'sources': ['CNBC'], 'evidence': {'CNBC': quote}}]})))
    async def review(*args, **kwargs):
        prompt = kwargs['messages'][0]['content']
        pairs = json.loads(prompt.split('Pairs:\n')[1])
        assert len(pairs) == 1
        assert pairs[0]['claim'] == f'CNBC writes: “{quote}”'
        return reply({'supported': [pairs[0]['id']]})
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(side_effect=review))
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(support, 'log_usage', Mock())
    result = await workflow.extract_and_compare_node({'topic': 'Jio IPO in October 2026',
        'sources': ['CNBC'], 'search_results': {'CNBC': original}})
    assert [c['claim'] for c in result['claims']] == [f'CNBC writes: “{quote}”']
    assert 'October 2026' not in result['claims'][0]['claim']


@pytest.mark.parametrize(('header', 'body', 'claim', 'accepted'), [
    ('Original: https://www.cbsnews.com/2026/a-report/\n',
     'The war has cost the military at least $47.1 billion as of mid-October.',
     'The war cost at least $47.1 billion by mid-October 2026.', False),
    ('Published: 2026-10-28T06:00:00+00:00\n',
     'The war has cost the military at least $47.1 billion as of mid-October.',
     'The war cost at least $47.1 billion by mid-October 2026.', True),
    ('', 'The company reported lower sales in October 2026.',
     'The company reported lower sales in October 2026.', True),
    ('Published: 2026-10-28T06:00:00+00:00\n', 'The company reported lower sales last year.',
     'The company reported lower sales in 2025.', True),
])
def test_calendar_year_requires_publisher_evidence_not_url_or_requested_topic(header, body, claim, accepted):
    reporting = {'Outlet': 'Retrieved publisher excerpts.\n' + header + 'Excerpt:\n' + body}
    result = workflow._ground_claim_attributions([{'claim': claim, 'sources': ['Outlet'],
        'evidence': {'Outlet': body}}], reporting)
    assert bool(result) is accepted


def test_published_year_cannot_supply_an_event_subperiod_missing_from_that_outlet():
    body = 'The cost of the war has gone up to $47 billion since last month.'
    text = 'Stored publisher excerpt.\nPublished: 2026-10-28T06:00:00Z\nExcerpt:\n' + body
    claim = {'claim': 'War costs reached at least $47 billion by mid-October 2026.',
             'sources': ['Outlet'], 'evidence': {'Outlet': body}}
    assert workflow._ground_claim_attributions([claim], {'Outlet': text}) == []
    claim['claim'] = 'War costs reached at least $47 billion by mid-Oct 2026.'
    with_period = text + ' These are the costs as of the middle of October.'
    assert workflow._ground_claim_attributions([claim], {'Outlet': with_period})


def test_explicit_fiscal_and_quarter_year_labels_are_valid_publisher_evidence():
    assert workflow._publisher_years('', 'The report covers FY2026 and Q32025.') == {'2026', '2025'}


def test_postfix_estimate_cannot_be_trimmed_into_an_unqualified_cost_result():
    quote = 'The war has cost the military at least $47.1 billion as of mid-October'
    body = quote + ', according to a new estimate from Central Command.'
    assert workflow._drops_numeric_estimate('The war cost at least $47.1 billion.', quote, body)
    assert workflow._drops_numeric_estimate('The war cost at least $47.1 billion.', body, body)
    assert not workflow._drops_numeric_estimate('Estimated war costs reached at least $47.1 billion.', quote, body)
    actual = 'The published cost was $29 billion.'
    assert not workflow._drops_numeric_estimate('The cost was $29 billion.', actual,
        actual + ' According to a new estimate, next year will cost more.')


def test_restoring_a_dependent_opening_cannot_create_an_unfinished_clause():
    body = 'No matter how often the candidate invokes Kennedy, the comparison misses the point.'
    assert support._restore_repair_context('the candidate invokes Kennedy', body) is None


@pytest.mark.asyncio
async def test_longer_original_sentence_can_preserve_full_legal_context(monkeypatch):
    original = ("A federal grand jury in Washington, D.C., on Tuesday rejected the Justice Department's "
                "attempt to indict former White House aide Cassidy Hutchinson over her testimony to "
                "lawmakers about President Trump's actions during the Jan. 6, 2021, attack on the Capitol.")
    assert len(original.split()) > 30
    monkeypatch.setattr(support, 'budgeted_message', AsyncMock(return_value=reply({
        'supported': [], 'outlet_passage': original})))
    monkeypatch.setattr(support, 'log_usage', Mock())
    result, limited = await support.verify_claim_support(Mock(), [], {'The Hill': original},
        topic='Cassidy Hutchinson indictment rejected', repair_missing=True)
    assert not limited
    assert result[0]['evidence']['The Hill'] == original


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
