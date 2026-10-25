import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import RSSArticle
from services import source_review as sr
from services.quality_gate import gate_summary
from services.summarizer import _review_article_results
from services.term_references import SECTION_338_DEFINITION


def reply(data, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text",text=json.dumps(data))],
        stop_reason=stop,usage=SimpleNamespace(input_tokens=10,output_tokens=20))


@pytest.mark.parametrize("data", [
    [{"i":1,"v":"pass"},{"i":1,"v":"retry"}],
    [{"i":2,"v":"pass"}], [{"i":1,"v":"probably"}],
])
def test_unknown_or_shifted_verdicts_cannot_authorize_copy(data):
    with pytest.raises((ValueError, sr.AlignmentError)):
        sr.parse_reviews(json.dumps(data),1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget","malformed","truncated","provider"])
async def test_unavailable_review_is_an_error_not_a_pass(monkeypatch,failure):
    monkeypatch.setattr(sr,'check_budget',AsyncMock(return_value=SimpleNamespace(allowed=failure!='budget')))
    client=AsyncMock()
    client.messages.create.return_value=reply([{"i":1,"v":"pass"}], 'max_tokens' if failure=='truncated' else 'end_turn')
    if failure=='malformed':
        client.messages.create.return_value=reply({"wrong":"shape"})
    if failure=='provider':
        client.messages.create.side_effect=RuntimeError('unavailable')
    out=await sr.assess_copy([{'id':'a','copy':'A fact.','source':'Evidence.'}],client=client)
    assert out['a']['v']=='error'
    if failure=='budget':
        client.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_verdicts_are_mapped_by_index_not_output_order(monkeypatch):
    monkeypatch.setattr(sr,'check_budget',AsyncMock(return_value=SimpleNamespace(allowed=True)))
    client=AsyncMock()
    client.messages.create.return_value=reply([{'i':2,'v':'retry'},{'i':1,'v':'pass'}])
    out=await sr.assess_copy([{'id':'a','copy':'One.'},{'id':'b','copy':'Two.'}],client=client)
    assert out['a']['v']=='pass' and out['b']['v']=='retry'


@pytest.mark.asyncio
@pytest.mark.parametrize(('verdict','flag'), [('retry','_degraded'),('error','_degraded'),('unavailable','_unavailable')])
async def test_article_review_rejections_cannot_reach_paid_enrichment(monkeypatch,verdict,flag):
    article=RSSArticle(title='Machado returns',source_url='https://example.com/a',source_name='FT',raw_content='The Nobel laureate returned.')
    monkeypatch.setattr('services.summarizer.assess_copy',AsyncMock(return_value={article.source_url:{'v':verdict}}))
    results={article.source_url:{'summary':'Edmundo returned.','category':'world'}}
    await _review_article_results(results,[article])
    assert results[article.source_url]=={'summary':'','category':'general',flag:True}


@pytest.mark.asyncio
async def test_story_rejection_keeps_the_cluster_retryable(monkeypatch):
    from services import story_synthesizer as ss
    monkeypatch.setattr(ss,'assess_copy',AsyncMock(return_value={'story':{'v':'retry'}}))
    client=AsyncMock()
    client.messages.create.return_value=reply({'headline':'Lakers update','summary':'Jake LaRiver is injured.','framings':[]})
    a={'title':'Jake LaRavia update','summary':'LaRavia was injured.','source_name':'A','source_url':'https://example.com/a'}
    out=await ss.synthesize_story([a,{**a,'source_name':'B'}],client=client)
    assert out['_failed'] is True


@pytest.mark.parametrize('text',[
    "Wisconsin's offensive snap counts reveal insights about the team's performance.",
    "A new poll shows a significant shift in voters' views.",
    "The Lakers announced a disappointing update regarding Jake LaRavia's status.",
    "Forbes provides hints and answers for the Connections puzzle on October 26.",
])
def test_observed_filler_and_guide_announcements_are_withheld(text):
    assert gate_summary(text)==''


@pytest.mark.parametrize('text',[
    "Wisconsin defeated No. 16 USC.",
    "Snap counts reveal insights about performance: Jones played 42 snaps.",
    "A new poll shows a significant shift in voters' views: support rose to 55%.",
    "The Lakers announced a disappointing update: LaRavia was ruled out.",
    "Forbes forecasts that revenue will grow 12% next year.",
])
def test_real_results_metrics_updates_and_forecasts_survive(text):
    assert gate_summary(text)==text


def test_statutory_definition_is_grounded_in_the_cited_reference():
    from services.primer_generator import _parse_primers
    article={'source_url':'https://example.com/a','title':'Section 338 tariffs changed','summary':'Tariffs changed.'}
    out=_parse_primers(json.dumps([{'i':1,'b':'','t':[{'term':'Section 338 tariffs','def':'Tariffs for national security or trade reasons.'}]}]),[article])
    assert out[article['source_url']]['terms'][0]['definition']==SECTION_338_DEFINITION
    assert len(SECTION_338_DEFINITION.split())<=25


def test_committed_semantic_cases_cover_the_failures_and_retention_controls():
    rows=json.loads((Path(__file__).parents[1]/'data/eval/source_fidelity_regressions.json').read_text())
    prompt=sr.build_review_prompt(rows)
    encoded=json.loads(prompt.split('INPUTS:\n',1)[1])
    assert [x['proposed_copy'] for x in encoded]==[x['copy'] for x in rows]
    assert {row['expected'] for row in rows}=={'pass','retry','unavailable'}
    assert 'relationships' in prompt and 'inherited verbatim' in prompt


@pytest.mark.asyncio
async def test_standalone_audit_opens_the_cost_ledger_pool_and_always_closes_it(monkeypatch):
    from scripts import audit_source_copy as audit
    from app import db
    pool=AsyncMock()
    monkeypatch.setattr(audit.asyncpg,'create_pool',AsyncMock(return_value=pool))
    monkeypatch.setattr(audit.settings,'ai_cost_guard_enabled',False)
    monkeypatch.setattr(audit.settings,'daily_ai_cost_limit_usd',10)
    async def assess(rows):
        assert db._pool is pool
        assert audit.settings.ai_cost_guard_enabled is True
        assert audit.settings.daily_ai_cost_limit_usd==15
        return {r['id']:{'v':r['expected']} for r in rows}
    monkeypatch.setattr(audit,'assess_copy',assess)
    assert await audit.main(SimpleNamespace(regressions=True,daily_cap=15)) is False
    pool.close.assert_awaited_once()
    assert db._pool is None
