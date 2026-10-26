import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import RSSArticle
from services import summarizer

URL = "https://example.com/game"
SOURCE = RSSArticle(title="College team makes history after Texas win", source_url=URL,
    source_name="Outlet", raw_content="Texas A&M demolished Texas in front of 1,500 spectators in Houston.")
GOOD = "Texas A&M defeated Texas in Houston in front of 1,500 spectators."
BAD = "Texas A&M made history in a remarkable victory that changed college hockey."


def response(copy):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps([
        {"i": 1, "s": copy, "c": "sports"}]))], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=20, output_tokens=20))


@pytest.mark.asyncio
async def test_changed_repair_is_parsed_and_independently_approved(monkeypatch):
    client = AsyncMock()
    client.messages.create.side_effect = [response(BAD), response(GOOD)]
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    review = AsyncMock(side_effect=[{URL: {"v": "retry", "why": "missing actual result"}}, {URL: {"v": "pass"}}])
    monkeypatch.setattr(summarizer, "assess_copy", review)
    out = await summarizer.summarize_articles([SOURCE], client=client)
    assert out[URL] == {"summary": GOOD, "category": "sports"}
    assert review.call_args_list[1].args[0][0]["copy"] == GOOD
    assert "missing actual result" in client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert client.messages.create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fresh_rejection", "fresh_unavailable", "review_error", "unsupported_figure", "unchanged", "budget"])
async def test_failed_or_unreviewed_repair_stays_pending_without_a_loop(monkeypatch, failure):
    candidate = GOOD
    if failure == "unsupported_figure":
        candidate = "Texas A&M defeated Texas in front of 9,999 spectators."
    if failure == "unchanged":
        candidate = BAD.lower() + "!"
    client = AsyncMock()
    client.messages.create.side_effect = [response(BAD), response(candidate)]
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(side_effect=[
        SimpleNamespace(allowed=True), SimpleNamespace(allowed=failure != "budget")]))
    review = AsyncMock(side_effect=[{URL: {"v": "retry", "why": "unsupported record"}},
                                   {URL: {"v": "error" if failure == "review_error" else "unavailable" if failure == "fresh_unavailable" else "retry"}}])
    monkeypatch.setattr(summarizer, "assess_copy", review)
    out = await summarizer.summarize_articles([SOURCE], client=client)
    assert out[URL] == {"summary": "", "category": "general", "_degraded": True}
    assert client.messages.create.await_count == (1 if failure == "budget" else 2)
    assert review.await_count == (1 if failure in {"unsupported_figure", "unchanged", "budget"} else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["unavailable", "error"])
async def test_unavailable_source_or_unavailable_review_never_triggers_rewrite(monkeypatch, verdict):
    client = AsyncMock()
    client.messages.create.return_value = response(BAD)
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(summarizer, "assess_copy", AsyncMock(return_value={URL: {"v": verdict}}))
    out = await summarizer.summarize_articles([SOURCE], client=client)
    assert not out[URL]["summary"]
    assert client.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_repair_review_cannot_discard_an_already_approved_neighbor(monkeypatch):
    neighbor = SOURCE.model_copy(update={"source_url": "https://example.com/neighbor"})
    client = AsyncMock()
    # The original response is aligned for both inputs; only the first needs repair.
    initial = response(BAD)
    initial.content[0].text = json.dumps([{"i": 1, "s": BAD, "c": "sports"},
                                         {"i": 2, "s": GOOD, "c": "sports"}])
    client.messages.create.side_effect = [initial, response(GOOD)]
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    review = AsyncMock(side_effect=[{URL: {"v": "retry"}, neighbor.source_url: {"v": "pass"}},
                                   {URL: {"v": "pass"}}])
    monkeypatch.setattr(summarizer, "assess_copy", review)
    out = await summarizer.summarize_articles([SOURCE, neighbor], client=client)
    assert out[URL]["summary"] == GOOD
    assert out[neighbor.source_url]["summary"] == GOOD
    assert [x["id"] for x in review.call_args_list[1].args[0]] == [URL]


@pytest.mark.asyncio
async def test_malformed_review_retries_once_before_a_verdict_is_used(monkeypatch):
    from services import source_review
    bad = SimpleNamespace(content=[SimpleNamespace(type="text", text='[{"i":1,"v" "pass"}]')],
                          stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    good = SimpleNamespace(content=[SimpleNamespace(type="text", text='[{"i":1,"v":"retry"}]')],
                           stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    client = AsyncMock()
    client.messages.create.side_effect = [bad, good]
    budget = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(source_review, "check_budget", budget)
    out = await source_review.assess_copy([{"id": "a", "copy": "An unsupported claim."}], client=client)
    assert out["a"]["v"] == "retry"
    assert client.messages.create.await_count == 2
    assert budget.await_count == 2


@pytest.mark.asyncio
async def test_malformed_review_cannot_retry_past_a_budget_stop(monkeypatch):
    from services import source_review
    client = AsyncMock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(type="text", text='not JSON')],
        stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=20))
    monkeypatch.setattr(source_review, "check_budget", AsyncMock(side_effect=[
        SimpleNamespace(allowed=True), SimpleNamespace(allowed=False)]))
    out = await source_review.assess_copy([{"id": "a", "copy": "A candidate."}], client=client)
    assert out["a"]["v"] == "error"
    assert client.messages.create.await_count == 1
