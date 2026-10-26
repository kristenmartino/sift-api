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


@pytest.fixture(autouse=True)
def stub_paid_event_review(monkeypatch):
    # These write-path tests inject synthesis; isolate the new paid boundary too.
    monkeypatch.setattr("workflows.incremental_threading.same_event", AsyncMock(return_value=True))

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
    pool.fetch.side_effect=[[dict(A.model_dump(), id="old", summary=A.raw_content)], [], [dict(A.model_dump(), id="a", summary=A.raw_content)]]
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
    ("The deal is $6 billion.", "The deal is $6bn.", set()),
    ("The deal is $6B.", "The deal is $6 bln.", set()),
    ("The deal is $6 million.", "The deal is $6bn.", {"6 million"}),
    ("The deal is $7 billion.", "The deal is $6bn.", {"7 billion"}),
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
        if "synthesis_attempts <" in sql:
            return [{"id":"old"}]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", A.title])
async def test_thin_rss_update_cannot_erase_saved_retry_evidence(monkeypatch, body):
    incoming=A.model_copy(update={"raw_content":body})
    monkeypatch.setattr("services.generation_recovery.pending_articles",AsyncMock(return_value=[A]))
    monkeypatch.setattr("services.deduplicator.deduplicate",AsyncMock(return_value=[]))
    out=await pipeline.deduplicate_node({"articles":[incoming]})
    assert out["new_articles"] == [A]

@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "old_hash", "new_hash", "body", "retry"), [
    ("unavailable", "old", "new", A.raw_content, True),
    ("unavailable", "same", "same", A.raw_content, False),
    ("unavailable", "old", "new", "", False),
    ("complete", "old", "new", A.raw_content, False),
])
async def test_only_changed_usable_unavailable_entries_bypass_url_dedup(monkeypatch, status, old_hash, new_hash, body, retry):
    from services.deduplicator import deduplicate
    pool=AsyncMock()
    pool.fetch.return_value=[{"source_url":A.source_url,"content_hash":old_hash,"generation_status":status}]
    monkeypatch.setattr("services.deduplicator.get_pool",AsyncMock(return_value=pool))
    article=A.model_copy(update={"content_hash":new_hash,"raw_content":body})
    assert await deduplicate([article]) == ([article] if retry else [])


@pytest.mark.parametrize(("summary", "conflict"), [
    ("Bon Secours is nearing a $6 billion takeover by Bain-backed Surgery Partners.", True),
    ("Bon Secours is nearing an acquisition by Surgery Partners.", True),
    ("Bon Secours is nearing a takeover of Surgery Partners.", False),
    ("Surgery Partners is nearing a takeover by Bon Secours.", False),
    ("The takeover by Bon Secours of Surgery Partners is in talks.", False),
    ("Bon Secours denied being the target of a takeover by Surgery Partners.", False),
])
def test_takeover_roles_preserve_active_passive_and_denied_claims(summary, conflict):
    from services.quality_gate import takeover_role_conflict
    title="Bon Secours nears $6bn takeover of Bain-backed Surgery Partners"
    assert takeover_role_conflict(summary,title) is conflict


def test_article_parser_rejects_the_observed_takeover_reversal():
    import json
    from services.summarizer import _parse_summaries
    from services.index_alignment import AlignmentError
    article=A.model_copy(update={"title":"Bon Secours nears $6bn takeover of Bain-backed Surgery Partners",
                                 "raw_content":"US hospital chain seeks a large deal."})
    with pytest.raises(AlignmentError, match="reversed takeover roles"):
        _parse_summaries(json.dumps([{"i":1,"s":"Bon Secours is nearing a $6 billion takeover by Bain-backed Surgery Partners.","c":"business"}]),[article])


@pytest.mark.asyncio
async def test_membership_moves_invalidate_only_stories_that_lose_inputs():
    from workflows.incremental_threading import _move_articles
    pool=AsyncMock()
    await _move_articles(pool,["moved-member"],"target")
    query,target,ids=pool.fetch.call_args.args
    assert target == "target" and ids == ["moved-member"]
    assert "FOR UPDATE" in query and "prior JOIN moved" in query
    assert "prior.story_id <> $1" in query
    assert "synthesis_status = 'failed'" in query and "copy_rules_version = NULL" in query
    assert "a.id <> ALL($2::text[])" in query


@pytest.mark.parametrize(("text", "conflict"), [
    ("The Buccaneers defeated the Panthers 33-6 at halftime.", True),
    ("The Buccaneers defeated Carolina 33–6 at half-time.", True),
    ("The Buccaneers led the Panthers 33-6 at halftime.", False),
    ("The Buccaneers defeated the Panthers 33-6.", False),
    ("The Buccaneers led 33-6 at halftime and defeated the Panthers 40-20.", False),
])
def test_halftime_leads_cannot_be_completed_wins(text, conflict):
    from services.quality_gate import interim_result_conflict
    assert interim_result_conflict(text) is conflict


def test_article_parser_reasks_observed_halftime_finality_error():
    import json
    from services.summarizer import _parse_summaries
    from services.index_alignment import AlignmentError
    article=A.model_copy(update={"title":"Buccaneers lead Panthers at halftime",
                                 "raw_content":"The Bucs led the Panthers 33-6 at halftime."})
    with pytest.raises(AlignmentError, match="halftime lead"):
        _parse_summaries(json.dumps([{"i":1,"s":"The Buccaneers defeated the Panthers 33-6 at halftime.","c":"sports"}]),[article])


@pytest.mark.asyncio
async def test_story_rejects_observed_halftime_finality_error(monkeypatch):
    import json
    from services.story_synthesizer import synthesize_story
    client=AsyncMock()
    client.messages.create.return_value=SimpleNamespace(
        content=[SimpleNamespace(type="text",text=json.dumps({"headline":"Buccaneers lead Panthers",
            "summary":"The Buccaneers defeated the Panthers 33-6 at halftime.","framings":[]}))],
        stop_reason="end_turn",usage=SimpleNamespace(input_tokens=10,output_tokens=20,cache_creation_input_tokens=0,cache_read_input_tokens=0))
    article={"title":"Buccaneers lead Panthers at halftime","summary":"The Bucs led 33-6 at halftime.","source_name":"A","source_url":"https://example.com/a"}
    out=await synthesize_story([article,{**article,"source_name":"B"}],client=client)
    assert out["_failed"] is True
