import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from workflows import compare_workflow as workflow


WIRED_TEXT = ('The repair company iFixit released its repairability rating for Apple’s very first '
              'foldable iPhone. It comes as users report a wave of cracked-screen issues.')
APPLE_ACKNOWLEDGMENT = ('Apple acknowledged the display issue and confirmed it affected a small '
                        'number of iPhone Duo units.')
DISPLAY_FAILURES = 'iPhone Duo users reported cracked inner screens after the retail launch.'


@pytest.mark.asyncio
async def test_short_wired_excerpt_does_not_inherit_other_outlets_claims(monkeypatch):
    reporting = {s: APPLE_ACKNOWLEDGMENT + ' ' + DISPLAY_FAILURES
                 for s in ['CNBC', 'Ars Technica', 'The Verge']}
    reporting['WIRED'] = ('Stored publisher excerpt (may be incomplete).\n'
                         'Headline: Some iPhone Duo Models Are Breaking\nExcerpt:\n' + WIRED_TEXT)
    acknowledgment = {'claim': APPLE_ACKNOWLEDGMENT, 'agreement': 'unanimous',
                      'sources': list(reporting), 'evidence': dict.fromkeys(reporting, APPLE_ACKNOWLEDGMENT)}
    failures = {'claim': DISPLAY_FAILURES, 'agreement': 'unanimous', 'sources': list(reporting),
                'evidence': {s: DISPLAY_FAILURES for s in reporting}}
    failures['evidence']['WIRED'] = 'It comes as users report a wave of cracked-screen issues.'
    payload = {'comparison': 'All four sources confirm Apple acknowledged the failures.',
               'claims': [acknowledgment, failures]}
    paid = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(payload))]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(workflow.anthropic, 'AsyncAnthropic', Mock())
    state = {'topic': 'Apple display failures', 'sources': list(reporting), 'search_results': reporting}
    extracted = await workflow.extract_and_compare_node(state)
    formatted = await workflow.format_response_node({**state, **extracted})
    by_claim = {c['claim']: c for c in formatted['claims']}
    assert by_claim[APPLE_ACKNOWLEDGMENT]['sources'] == ['CNBC', 'Ars Technica', 'The Verge']
    assert by_claim[APPLE_ACKNOWLEDGMENT]['agreement'] == 'majority'
    assert by_claim[DISPLAY_FAILURES]['sources'] == list(reporting)
    assert by_claim[DISPLAY_FAILURES]['agreement'] == 'unanimous'
    assert APPLE_ACKNOWLEDGMENT not in formatted['comparison']
    assert formatted['comparison'].startswith('4 of 4 retrieved outlets report:')
    assert all('evidence' not in c for c in formatted['claims'])
    paid.assert_awaited_once()
    assert paid.await_args.kwargs['max_tokens'] == 4096


@pytest.mark.parametrize('evidence', [None, {}, {'Unknown outlet': APPLE_ACKNOWLEDGMENT},
                                      {'WIRED': APPLE_ACKNOWLEDGMENT}, {'WIRED': 'users report'}])
def test_missing_borrowed_or_thin_quotes_never_credit_an_outlet(evidence):
    claims = [{'claim': APPLE_ACKNOWLEDGMENT, 'agreement': 'unique', 'sources': ['WIRED'], 'evidence': evidence}]
    assert workflow._ground_claim_attributions(claims, {'WIRED': WIRED_TEXT}) == []


def test_stored_headline_metadata_cannot_substitute_for_publisher_text():
    reporting = {'WIRED': 'Stored publisher excerpt (may be incomplete).\nHeadline: '
                 + APPLE_ACKNOWLEDGMENT + '\nExcerpt:\n' + WIRED_TEXT}
    claims = [{'claim': APPLE_ACKNOWLEDGMENT, 'sources': ['WIRED'],
               'evidence': {'WIRED': APPLE_ACKNOWLEDGMENT}}]
    assert workflow._ground_claim_attributions(claims, reporting) == []


def test_dispute_requires_passages_from_both_sides():
    supporting = 'The committee approved the measure by a vote of ten to two.'
    opposing = 'The committee rejected the measure by a vote of ten to two.'
    reporting = {'AP': supporting, 'Fox News': opposing}
    claim = {'claim': 'The committee approved the measure.', 'agreement': 'disputed',
             'sources_for': ['AP'], 'sources_against': ['Fox News'], 'evidence': {'AP': supporting}}
    assert workflow._ground_claim_attributions([claim], reporting) == []
    claim['evidence']['Fox News'] = opposing
    grounded = workflow._ground_claim_attributions([claim], reporting)
    assert grounded[0]['sources_for'] == ['AP']
    assert grounded[0]['sources_against'] == ['Fox News']


def test_summary_cannot_restore_rejected_unanimous_attribution():
    claims = [{'claim': APPLE_ACKNOWLEDGMENT, 'sources': ['CNBC', 'The Verge', 'Ars Technica']}]
    summary = workflow._grounded_comparison_summary(claims, ['CNBC', 'The Verge', 'Ars Technica', 'WIRED'])
    assert summary.startswith('3 of 4 retrieved outlets report:')


def test_typographic_quote_changes_do_not_drop_a_supported_claim():
    body = 'The committee’s chair said the measure would take effect on Monday.'
    claim = {'claim': 'The chair said the measure takes effect on Monday.', 'sources': ['NR'],
             'evidence': {'NR': "The committee's chair said the measure would take effect on Monday."}}
    assert workflow._ground_claim_attributions([claim], {'NR': body})[0]['sources'] == ['NR']
    claim['evidence']['NR'] = "The committee's chair said the measure would take effect on Friday."
    assert workflow._ground_claim_attributions([claim], {'NR': body}) == []


CORNELL_OPINION = ('As long as the premises of the sexual revolution remain unexamined, '
                   'young people will remain unprepared for the realities of sex and relationships.')
MICHIGAN_FRAGMENT = ('No matter how often Michigan Senate candidate Abdul El-Sayed invokes JFK, '
                     'Kennedy didn’t defend his Catholic faith as a way of distracting from his ow...')
MICHIGAN_CLAUSE = 'Michigan Senate candidate Abdul El-Sayed invokes JFK'


@pytest.mark.asyncio
@pytest.mark.parametrize(('body', 'passage'), [(CORNELL_OPINION, CORNELL_OPINION),
                                         (MICHIGAN_FRAGMENT, MICHIGAN_CLAUSE)])
async def test_omitted_short_outlet_contribution_survives_grouping_without_paraphrase(monkeypatch, body, passage):
    reporting = {'National Review': 'Retrieved publisher excerpts (may be incomplete).\nExcerpt:\n' + body,
                 'CBS News': APPLE_ACKNOWLEDGMENT}
    payload = {'outlet_passages': {'National Review': passage, 'CBS News': APPLE_ACKNOWLEDGMENT},
               'claims': [{'claim': APPLE_ACKNOWLEDGMENT, 'sources': ['CBS News'],
                           'evidence': {'CBS News': APPLE_ACKNOWLEDGMENT}}]}
    paid = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(payload))]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(workflow.anthropic, 'AsyncAnthropic', Mock())
    state = {'topic': 'short publisher excerpt', 'sources': list(reporting), 'search_results': reporting}
    extracted = await workflow.extract_and_compare_node(state)
    formatted = await workflow.format_response_node({**state, **extracted})
    contribution = next(c for c in formatted['claims'] if c.get('sources') == ['National Review'])
    assert contribution['claim'] == f'National Review writes: “{passage}”'
    assert contribution['agreement'] == 'unique'
    assert len(formatted['claims']) == 2
    paid.assert_awaited_once()
    assert paid.await_args.kwargs['max_tokens'] == 4096


@pytest.mark.parametrize('passages', [None, [], {'National Review': None},
    {'National Review': 'young people'}, {'National Review': APPLE_ACKNOWLEDGMENT},
    {'National Review': MICHIGAN_FRAGMENT},
    {'Unknown outlet': CORNELL_OPINION}])
def test_outlet_passages_cannot_pad_with_missing_borrowed_or_unfinished_evidence(passages):
    assert workflow._retain_outlet_passages([], passages, {'National Review': MICHIGAN_FRAGMENT}) == []


def test_outlet_passage_cannot_use_headline_as_evidence():
    text = 'Stored publisher excerpt (may be incomplete).\nHeadline: ' + CORNELL_OPINION + '\nExcerpt:\n'
    assert workflow._retain_outlet_passages([], {'National Review': CORNELL_OPINION}, {'National Review': text}) == []


def test_represented_outlet_does_not_get_duplicate_fallback():
    claim = {'claim': CORNELL_OPINION, 'sources': ['National Review']}
    assert workflow._retain_outlet_passages([claim], {'National Review': CORNELL_OPINION},
                                            {'National Review': CORNELL_OPINION}) == [claim]


def test_eight_claims_from_rich_outlet_do_not_crowd_out_thin_excerpt():
    claims = [{'claim': f'Fact {i}', 'sources': ['CBS News']} for i in range(8)]
    result = workflow._retain_outlet_passages(claims, {'National Review': CORNELL_OPINION},
                                              {'National Review': CORNELL_OPINION, 'CBS News': APPLE_ACKNOWLEDGMENT})
    assert len(result) == 8
    assert result[0] == claims[0]
    assert result[-1]['sources'] == ['National Review']
    assert result[-1]['claim'] == f'National Review writes: “{CORNELL_OPINION}”'


def test_coverage_reservation_keeps_five_outlets_and_prioritizes_disputes():
    claims = [{'claim': f'Fact {i}', 'sources': ['A']} for i in range(7)]
    claims += [{'claim': 'Dispute', 'agreement': 'disputed', 'sources_for': ['A'], 'sources_against': ['B']}]
    reporting = dict.fromkeys(['A', 'B', 'C', 'D', 'E'], APPLE_ACKNOWLEDGMENT)
    result = workflow._retain_outlet_passages(claims, dict.fromkeys(reporting, APPLE_ACKNOWLEDGMENT), reporting)
    assert len(result) == 8
    assert any(c.get('agreement') == 'disputed' for c in result)
    assert set().union(*(set(c.get('sources', []) + c.get('sources_for', []) + c.get('sources_against', []))
                         for c in result)) == set(reporting)


def test_complete_reviewed_short_editorial_survives_even_null_model_assessment():
    text = 'Stored publisher excerpt (may be incomplete).\nHeadline: Cornell ban\nExcerpt:\n' + CORNELL_OPINION
    result = workflow._retain_outlet_passages([], {'National Review': None}, {'National Review': text})
    assert result[0]['claim'] == f'National Review writes: “{CORNELL_OPINION}”'


@pytest.mark.parametrize('body', [MICHIGAN_FRAGMENT, MICHIGAN_FRAGMENT.replace('...', '…'),
                                  'Cornell University fraternity party ban'])
def test_incomplete_short_excerpt_has_no_automatic_contribution(body):
    text = 'Stored publisher excerpt (may be incomplete).\nExcerpt:\n' + body
    assert workflow._retain_outlet_passages([], {'National Review': None}, {'National Review': text}) == []
