"""Live-feed copy failures: remove padding without deleting reported detail."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.quality_gate import clean_generated_headline, gate_framing, gate_summary
from services.story_synthesizer import synthesize_story


@pytest.mark.parametrize('closer', [
    'The development signals escalating military tensions between the two countries.',
    'The incident marks a notable event at a historically significant site.',
    'The finding raises questions about health consequences.',
])
def test_generic_closer_is_removed_but_the_event_is_kept(closer):
    fact = 'One person was arrested after a fire at the retreat.'
    assert gate_summary(fact + ' ' + closer) == fact


@pytest.mark.parametrize('summary', [
    'The move raises concerns cited by the regulator about unreported transfers.',
    'The proposal raises concerns about a $6.4 billion charge to ratepayers.',
    'The move signals what the minister called "a significant shift" in policy.',
    "The move signals 'a significant shift' in policy, the minister said.",
    'A study found elevated concern among participants who had lost their jobs.',
    'The decision marks a shift from voluntary to mandatory reporting.',
    'The proposal reflects concerns voiced by farmers about water allocations.',
])
def test_specific_figures_attribution_and_quoted_language_survive(summary):
    assert gate_summary(summary) == summary


def test_headline_retains_uncertainty_but_not_duplicate_generic_attribution():
    assert clean_generated_headline('Officials reportedly plan talks, sources say') == 'Officials reportedly plan talks'
    assert clean_generated_headline('Trial reportedly halted, according to sources') == 'Trial reportedly halted'
    assert clean_generated_headline('Trial reportedly halted, ministry says') == 'Trial reportedly halted, ministry says'
    assert clean_generated_headline('Trial halted, sources say') == 'Trial halted, sources say'


@pytest.mark.parametrize('note', [
    'Does not specify France as the location, unlike other sources.',
    'Notes the article does not specify conditions that would elevate the risk.',
    "Describes the event as a hospital scare without specifying a fall.",
])
def test_silence_in_a_short_input_does_not_establish_an_outlet_omission(note):
    assert gate_framing(note, source_text='The actor was taken to hospital after a fall.') == ''


def test_an_explicitly_reported_omission_is_not_lost():
    fact = 'The court ruled without addressing the constitutional challenge.'
    assert gate_framing(fact, source_text=fact) == fact


@pytest.mark.asyncio
async def test_synthesis_applies_copy_guards_and_preserves_empty_source_row():
    data = {'headline': 'Officials reportedly plan talks, sources say',
            'summary': 'Officials plan talks. The move signals broader tensions.',
            'framings': [{'source_name': 'A', 'framing': 'Does not specify a date.', 'tone': 'critical'}]}
    client = AsyncMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type='text', text=json.dumps(data))], stop_reason='end_turn',
        usage=SimpleNamespace(input_tokens=10, output_tokens=10))
    articles = [{'title': 'Talks planned', 'summary': 'Officials plan talks.',
                 'source_name': name, 'source_url': 'https://example.com/' + name} for name in ['A', 'B']]
    result = await synthesize_story(articles, client=client)
    assert result['headline'] == 'Officials reportedly plan talks'
    assert result['summary'] == 'Officials plan talks.'
    assert result['framings'] == [{'source_name': 'A', 'framing': '', 'tone': 'neutral'}]
