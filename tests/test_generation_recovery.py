"""Recovery survives URL dedup, provider failure and missing source evidence."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import RSSArticle
from services import primer_generator as pg
from services.copy_rules import COPY_RULES_VERSION
from services.judge import build_judge_prompt
from services.summarizer import _raw_content_fallback
from workflows import pipeline_workflow as pipeline
from workflows.incremental_threading import _attach

A = RSSArticle(title="Bank holds rate", source_url="https://example.com/a", source_name="AP",
               raw_content="The central bank held its rate at 4.25% on Tuesday.")

@pytest.mark.asyncio
async def test_retry_survives_existing_url_and_missing_rss(monkeypatch):
    monkeypatch.setattr("services.generation_recovery.pending_articles", AsyncMock(return_value=[A]))
    monkeypatch.setattr("services.deduplicator.deduplicate", AsyncMock(return_value=[]))
    result = await pipeline.deduplicate_node({"articles": []})
    assert result["new_articles"] == [A]

@pytest.mark.asyncio
async def test_retry_prefers_untruncated_fresh_rss(monkeypatch):
    fresh = A.model_copy(update={"raw_content": A.raw_content + " The vote was unanimous."})
    monkeypatch.setattr("services.generation_recovery.pending_articles", AsyncMock(return_value=[A]))
    monkeypatch.setattr("services.deduplicator.deduplicate", AsyncMock(return_value=[]))
    result = await pipeline.deduplicate_node({"articles": [fresh]})
    assert result["new_articles"] == [fresh]

@pytest.mark.asyncio
async def test_budget_fallback_is_retryable_and_does_not_get_optional_enrichment(monkeypatch):
    fallback = _raw_content_fallback([A])
    assert fallback[A.source_url]["_degraded"]
    submit = AsyncMock()
    monkeypatch.setattr("services.primer_generator.submit_primer_batch", submit)
    await pipeline.primer_node({"new_articles": [A], "summaries": fallback})
    submit.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "status"), [(A.raw_content, "pending"), ("", "unavailable")])
async def test_store_keeps_source_for_retry_and_never_versions_a_fallback(monkeypatch, body, status):
    pool = AsyncMock()
    monkeypatch.setattr("app.db.get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("services.entity_extractor.submit_entity_batch", AsyncMock())
    monkeypatch.setattr("services.generation_health.refresh", AsyncMock())
    monkeypatch.setattr("app.config.settings.incremental_threading_enabled", True)
    monkeypatch.setattr("workflows.incremental_threading.run_incremental_threading", AsyncMock())
    article = A.model_copy(update={"raw_content": body, "category": "general"})
    await pipeline.store_node({"new_articles": [article], "summaries": _raw_content_fallback([article])})
    insert = next(c for c in pool.execute.call_args_list if "INSERT INTO articles" in c.args[0])
    assert insert.args[-3:] == (body, status, None)
    assert "THEN NULL ELSE articles.threaded_at" in insert.args[0]

@pytest.mark.asyncio
async def test_duplicate_background_is_compared_with_retained_definitions(monkeypatch):
    calls=[]
    async def review(items, field, **kwargs):
        calls.append((field, items))
        return [{"id": x["id"], "judged": True, "restates": field == "background_novelty",
                 "adds_significance": field != "background_novelty", "neutral_cliche_free": True} for x in items]
    monkeypatch.setattr(pg, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(pg, "judge_lines", review)
    term={"term": "denaturalization", "definition": "Revocation of citizenship through a legal proceeding."}
    payload={A.source_url: {"background": "A court can revoke citizenship in a denaturalization case.", "terms": [term]}}
    await pg._review_backgrounds(payload, [{**A.model_dump(), "summary": "An opinion column discussed denaturalization."}])
    assert payload[A.source_url] == {"background": "", "terms": [term], "copy_rules_version": COPY_RULES_VERSION}
    assert calls[1][1][0]["terms"] == [term]
    prompt=build_judge_prompt(calls[1][1], "background_novelty")
    assert term["definition"] in prompt

@pytest.mark.asyncio
async def test_obvious_term_is_removed_without_losing_useful_jargon(monkeypatch):
    async def review(items, **kwargs):
        return [{"id": x["id"], "judged": True, "restates": "stock low" in x["line"],
                 "adds_significance": "stock low" not in x["line"], "neutral_cliche_free": True} for x in items]
    monkeypatch.setattr(pg, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(pg, "judge_lines", review)
    good={"term":"basis points", "definition":"One hundredth of a percentage point."}
    payload={A.source_url:{"background":"", "terms":[{"term":"stock low","definition":"The lowest stock price."},good]}}
    await pg._review_backgrounds(payload,[{**A.model_dump(),"summary":"Rates fell 25 basis points as shares hit a stock low."}])
    assert payload[A.source_url]["terms"] == [good]

@pytest.mark.asyncio
async def test_same_outlet_does_not_reuse_old_rules(monkeypatch):
    pool=AsyncMock()
    pool.fetch.side_effect=[[{"source_name":"AP"}], [dict(A.model_dump(), id="a", summary=A.raw_content)]]
    pool.fetchval.return_value="old"
    synth=AsyncMock(return_value={"headline":"H","summary":"S","framings":[]})
    await _attach(pool,{"id":"a","source_name":"AP"},"story",synth)
    synth.assert_awaited_once()
    assert pool.execute.call_args.args[-1] == COPY_RULES_VERSION


def test_term_rubric_rejects_live_failure_and_retains_specialist_terms():
    prompt=build_judge_prompt([],"term_utility")
    assert '"stock low"' in prompt and "habeas corpus" in prompt

@pytest.mark.parametrize(("text", "source", "expected"), [
    ("The budget is £7 million.", "The budget is £7m.", set()),
    ("The cost is 7,000,000.", "The cost is 7 million.", set()),
    ("The cost is 8 million.", "The cost is 7m.", {"8 million"}),
    ("The rate is 7%.", "The rate is 7.", {"7%"}),
    ("The discount is 40%.", "The discount is 40 percent.", set()),
    ("The discount is 40 per cent.", "The discount is 40%.", set()),
    ("The discount is 41%.", "The discount is 40 percent.", {"41%"}),
    ("Version 1.2.3 shipped.", "Version 1.2.3 shipped.", set()),
    ("Version 1.2.4 shipped.", "Version 1.2.3 shipped.", {"1.2.4"}),
])
def test_equivalent_scales_do_not_block_recovery(text, source, expected):
    from services.quality_gate import unsupported_figures
    assert unsupported_figures(text, source) == expected

@pytest.mark.parametrize("text", [
    "Sports Illustrated provides viewing information for Baylor's Week 8 football game against Kansas.",
    "Sports Illustrated provides main card predictions for UFC 333 fights including Alexander Volkanovski vs. Merab Dvalishvili.",
    "Sports Illustrated analyzes the Eagles' Week 7 Monday Night Football matchup against the Cowboys.",
])
def test_preview_descriptions_do_not_become_news(text):
    from services.quality_gate import gate_summary
    assert gate_summary(text) == ""


def test_a_substantive_attributed_prediction_survives():
    from services.quality_gate import gate_summary
    text="Sports Illustrated predicts that the Eagles will win by seven points."
    assert gate_summary(text) == text


def test_background_review_rejects_guessed_policy_conditions():
    prompt=build_judge_prompt([],"background_novelty")
    assert "venue restrictions" in prompt and "facility-use rules" in prompt


def test_explicit_unavailable_summary_keeps_its_aligned_index():
    import json
    from services.summarizer import _parse_summaries
    result=_parse_summaries(json.dumps([{"i":1,"s":"","c":"general","u":True}]),[A])
    assert result[A.source_url] == {"summary":"", "category":"general", "_unavailable":True}


def test_concrete_forecast_is_not_a_preview():
    from services.quality_gate import gate_summary
    text="The Fed provides predictions of 2% inflation in 2027."
    assert gate_summary(text) == text


@pytest.mark.asyncio
async def test_failed_legacy_refresh_is_not_left_published():
    from workflows.incremental_threading import _sweep_failed
    pool=AsyncMock()
    pool.fetchval.return_value=0
    async def fetch(sql,*args):
        if "synthesis_attempts <" in sql:return [{"id":"old"}]
        return [dict(A.model_dump(), id="a", summary=A.raw_content),
                dict(A.model_dump(), id="b", summary=A.raw_content, source_name="Reuters")]
    pool.fetch.side_effect=fetch
    import workflows.incremental_threading as workflow
    from unittest.mock import patch
    with patch.object(workflow,"check_budget",AsyncMock(return_value=SimpleNamespace(allowed=True))):
        await _sweep_failed(pool,AsyncMock(return_value={"_failed":True}))
    query=pool.execute.call_args.args[0]
    assert "THEN 'failed' ELSE synthesis_status" in query
    assert "updated_at" not in query
    assert pool.execute.call_args.args[-1] == COPY_RULES_VERSION
