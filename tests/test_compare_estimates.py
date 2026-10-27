import pytest

from workflows.compare_workflow import _ground_claim_attributions

BODY = ('Economists estimated that business spending on equipment grew at a roughly 20% annualized rate last quarter.'
        'Economists forecast the economy grew at more than a 3.0% rate in the third quarter. '
        'Actual core orders rose 0.5% in September, the department said.')
QUOTE = 'business spending on equipment grew at a roughly 20% annualized rate last quarter'


def claim(text, quote=QUOTE):
    return {'claim': text, 'agreement': 'unique', 'sources': ['Reuters'], 'evidence': {'Reuters': quote}}


@pytest.mark.parametrize('quote', [QUOTE, 'Economists estimated that ' + QUOTE])
def test_subclause_cannot_turn_an_unreleased_estimate_into_a_confirmed_result(quote):
    result = _ground_claim_attributions([claim('Equipment spending grew at a roughly 20% annualized rate.', quote)], {'Reuters': BODY})
    assert result == []


def test_keeping_estimate_attribution_retains_a_supported_claim():
    result = _ground_claim_attributions([claim('Economists estimated equipment spending grew at a roughly 20% annualized rate.')], {'Reuters': BODY})
    assert result[0]['sources'] == ['Reuters']


def test_decimal_forecast_retains_its_entire_sentence_scope():
    quote = 'the economy grew at more than a 3.0% rate in the third quarter'
    assert _ground_claim_attributions([claim('Third-quarter GDP growth exceeded 3.0%.', quote)], {'Reuters': BODY}) == []
    assert _ground_claim_attributions([claim('Economists forecast third-quarter GDP growth of more than 3.0%.', quote)], {'Reuters': BODY})


def test_actual_measurement_in_next_sentence_is_not_dropped_with_a_forecast():
    quote = 'Actual core orders rose 0.5% in September'
    assert _ground_claim_attributions([claim('Core orders rose 0.5% in September.', quote)], {'Reuters': BODY})


def test_estimate_guard_does_not_remove_another_outlets_actual_measurement():
    evidence = claim('Equipment spending grew at a roughly 20% annualized rate.')
    evidence['sources'].append('Example News')
    evidence['evidence']['Example News'] = QUOTE
    result = _ground_claim_attributions([evidence], {'Reuters': BODY, 'Example News': 'Official data showed ' + QUOTE + '.'})
    assert result[0]['sources'] == ['Example News']
