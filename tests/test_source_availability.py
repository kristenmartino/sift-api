import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import RSSArticle
from services import summarizer
from services.quality_gate import gate_summary

URL = "https://example.com/split"
ARTICLE = RSSArticle(title="Alex Vale and partner Jordan Park split after four years", source_name="Outlet",
                     source_url=URL, raw_content="Vale said the breakup came without all the answers.")
GOOD = "Alex Vale and partner Jordan Park split after four years together."


def reply(summary, unavailable=False):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps([
        {"i": 1, "s": summary, "c": "general" if unavailable else "entertainment", "u": unavailable}]))],
        stop_reason="end_turn", usage=SimpleNamespace(input_tokens=20, output_tokens=20))


@pytest.mark.asyncio
async def test_producer_cannot_silently_suppress_a_usable_story(monkeypatch):
    client = AsyncMock()
    client.messages.create.side_effect = [reply("", True), reply(GOOD)]
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    review = AsyncMock(side_effect=[{URL: {"v": "retry", "why": "named couple split is usable"}}, {URL: {"v": "pass"}}])
    monkeypatch.setattr(summarizer, "assess_copy", review)
    result = await summarizer.summarize_articles([ARTICLE], client=client)
    assert review.call_args_list[0].args[0][0]["review_mode"] == "source_availability"
    assert review.call_args_list[0].args[0][0]["copy"] == ""
    assert review.call_args_list[1].args[0][0]["review_mode"] == "copy"
    assert result[URL] == {"summary": GOOD, "category": "entertainment"}
    assert client.messages.create.await_count == 2
    assert '"source_has_usable_fact": true' in client.messages.create.call_args.kwargs["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("verdict", "flag"), [("unavailable", "_unavailable"), ("error", "_degraded"), ("pass", "_degraded")])
async def test_empty_copy_is_never_approved_or_generated_without_a_usable_source_verdict(monkeypatch, verdict, flag):
    client = AsyncMock()
    client.messages.create.return_value = reply("", True)
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(summarizer, "assess_copy", AsyncMock(return_value={URL: {"v": verdict}}))
    result = await summarizer.summarize_articles([ARTICLE], client=client)
    assert result[URL]["summary"] == ""
    assert result[URL][flag] is True
    assert client.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_availability_repair_respects_the_budget(monkeypatch):
    client = AsyncMock()
    client.messages.create.return_value = reply("", True)
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(side_effect=[SimpleNamespace(allowed=True), SimpleNamespace(allowed=False)]))
    monkeypatch.setattr(summarizer, "assess_copy", AsyncMock(return_value={URL: {"v": "retry"}}))
    result = await summarizer.summarize_articles([ARTICLE], client=client)
    assert result[URL]["_degraded"] is True
    assert client.messages.create.await_count == 1


@pytest.mark.parametrize(("before", "after"), [
    ("Purdue will host Maryland on November 7, with kickoff time and TV information released.", "Purdue will host Maryland on November 7."),
    ("The Giants defeated an opponent 27-21 in overtime with a 188-yard rushing day.", "The Giants won 27-21 in overtime with a 188-yard rushing day."),
    ('The coach said "we defeated an opponent 27-21".', 'The coach said "we defeated an opponent 27-21".'),
    ("The Giants defeated the Cowboys 27-21 in overtime.", "The Giants defeated the Cowboys 27-21 in overtime."),
])
def test_empty_teaser_tail_and_placeholder_are_removed_without_editing_quotes_or_named_opponents(before, after):
    assert gate_summary(before) == after


@pytest.mark.asyncio
async def test_cluster_audit_retains_the_publisher_evidence(monkeypatch):
    from app import db
    from scripts import audit_source_copy as audit
    pool = AsyncMock()
    pool.fetch.side_effect = [[], [{"id": "s", "headline": "Offering announced", "summary": "Financial Times reported an offering.", "framings": [{"source_name": "Financial Times", "framing": "Offering could dilute shareholders."}]}],
                              [{"story_id": "s", "source_name": "Financial Times", "title": "Offering planned", "summary": "The company plans an offering."}]]
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool))
    async def review(rows):
        assert "[Financial Times]" in rows[0]["source"]
        assert "Offering could dilute shareholders." in rows[0]["copy"]
        return {"s": {"v": "pass"}}
    monkeypatch.setattr(audit, "assess_copy", review)
    assert await audit._run_audit(SimpleNamespace(regressions=False, limit=1)) is False
