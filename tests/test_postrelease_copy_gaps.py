import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.quality_gate import unsupported_name_change,gate_summary,gate_framing,gate_generated_background
from services.summarizer import _parse_summaries
from services.index_alignment import AlignmentError


@pytest.mark.parametrize(('copy','source','bad'),[
 ('Jair Bolsonaro lost.','Bolsonaro lost.',True),
 ('Flávio Bolsonaro lost.','Bolsonaro lost.',True),
 ('Flávio Bolsonaro, son of Jair Bolsonaro, lost.','Flávio Bolsonaro is the son of Jair Bolsonaro.',False),
 ('Jake LaRiver was injured.','Jake LaRavia was injured.',True),
 ("Agility Robotics builds Digit.","Agility Robotic's humanoid robot Digit.",False),
 ('Turnstile Raises Funds','Turnstile Raise Funds',False),
 ("Jalen Hurts's treatment was reported.","Jalen Hurts was treated.",False),
 ('Alfonso Cuarón’s film returned.','Alfonso Cuarón&#8217;s film returned.',False),
 ('Federal Housing Finance Agency responded.','Federal Housing Finance Agency responded.',False),
 ('Luiz Inácio Lula da Silva won.','Lula won.',False),
])
def test_ambiguous_family_names_and_spelling_have_precise_controls(copy,source,bad):
 assert unsupported_name_change(copy,source) is bad


def test_article_parser_reasks_an_invented_family_first_name():
 a=SimpleNamespace(title='Lula wins runoff after markets favored Bolsonaro',source_name='CNBC',source_url='https://example.com/a',raw_content='Markets favored Bolsonaro, but Lula won.')
 with pytest.raises(AlignmentError,match='unsupported name'):
  _parse_summaries(json.dumps([{'i':1,'s':'Lula won despite odds favoring Jair Bolsonaro.','c':'world'}]),[a])


def test_generic_trailing_clause_is_removed_without_losing_the_reported_event():
 assert gate_summary("Anthropic's president criticized OpenAI over its Pentagon deal, escalating rivalry between two major artificial intelligence companies.")=="Anthropic's president criticized OpenAI over its Pentagon deal."


def test_attributed_or_quantified_trailing_findings_are_retained():
 text='The minister called the deal "escalating rivalry", according to the committee.'
 assert gate_summary(text)==text


@pytest.mark.parametrize(('note','summary','expected'),[
 ('No arrests had been made.','A suspect was arrested Sunday.',''),
 ('No arrests had been made.','Police continued searching for the assailant.','No arrests had been made.'),
 ('Earlier, no arrests had been made.','A suspect was arrested Sunday.','Earlier, no arrests had been made.'),
])
def test_outdated_status_notes_do_not_contradict_the_current_update(note,summary,expected):
 assert gate_framing(note,source_text=note,summary=summary)==expected


@pytest.mark.parametrize('paragraph',[
 'The agency oversees enterprises that guarantee roughly half of all mortgages.',
 'The office has a five-year term renewable once.',
 'The tournament includes four invited nations.',
 'These plans let employers pay only actual claims rather than fixed premiums.',
 'The Administrative Procedure Act constrains removal.',
])
def test_unsourced_primer_specifics_are_withheld(paragraph):
 assert gate_generated_background(paragraph,title='Agency update',summary='An agency issued a statement.')==''


def test_supported_count_and_stable_institutional_role_survive():
 assert gate_generated_background('The office has a five-year term.',title='Five-year term announced',summary='The term was announced.')
 assert gate_generated_background('The DNI coordinates U.S. intelligence agencies.',title='DNI issued memo',summary='The DNI issued a memo.')


def test_term_definitions_cannot_bypass_figure_or_clinical_precision_rules():
 from services.primer_generator import _parse_primers
 a={'title':'Late Triassic find and ADC drug','summary':'Researchers reported a find and a treatment.','source_url':'https://example.com/a'}
 out=_parse_primers(json.dumps([{'i':1,'t':[{'term':'Late Triassic','def':'A period from 252 to 201 million years ago.'},{'term':'ADC drug','def':'A targeted therapy that kills cancer cells while sparing healthy tissue.'}]}]),[a])
 assert not out


@pytest.mark.asyncio
async def test_source_name_is_supplied_to_the_semantic_reviewer(monkeypatch):
 from services import summarizer
 a=SimpleNamespace(title='An opinion',source_name='Washington Examiner',source_url='https://example.com/a',raw_content='An opinion about policy.')
 review=AsyncMock(return_value={a.source_url:{'v':'pass'}})
 monkeypatch.setattr(summarizer,'assess_copy',review)
 result={a.source_url:{'summary':'The Washington Examiner argued for a policy.','category':'politics'}}
 await summarizer._review_article_results(result,[a])
 assert review.call_args.args[0][0]['source_name']=='Washington Examiner'
