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
