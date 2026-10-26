import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import RSSArticle
from services import summarizer
from services.quality_gate import unsupported_figures


@pytest.mark.parametrize(("copy", "source", "expected"), [
    ("47 people", "Forty-seven people", set()),
    ("47 people", "Forty seven people", set()),
    ("47%", "forty-seven percent", set()),
    ("47 billion", "forty-seven billion", set()),
    ("19 people", "Nineteen people", set()),
    ("7 people", "forty-seven people", {"7"}),
    ("48 people", "forty-seven people", {"48"}),
    ("47%", "forty-seven people", {"47%"}),
    ("47 people", "forty-seven percent", {"47"}),
    ("47 people", "forty to seven people", {"47"}),
    ("A funding round at a lower valuation than the prior one.", "Down round", set()),
])
def test_source_count_equivalence_preserves_amount_and_units(copy, source, expected):
    assert unsupported_figures(copy, source) == expected


def article(n):
    return RSSArticle(title=f"Agency files charges in case {n}", source_name="Outlet",
                      source_url=f"https://example.com/{n}", raw_content="The agency charged 48 people. Forty-seven were residents.")


def response(rows):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(rows))],
                           stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=20))


@pytest.mark.asyncio
async def test_a_persistently_bad_article_does_not_block_its_aligned_neighbor(monkeypatch):
    a, b = article(1), article(2)
    good = "The agency filed charges against 48 people, including 47 residents."
    bad = "The agency filed charges against 49 people, including 47 residents."
    mixed = [{"i": 1, "s": bad, "c": "politics"}, {"i": 2, "s": good, "c": "politics"}]
    client = AsyncMock()
    client.messages.create.side_effect = [response(mixed), response(mixed),
        response([{"i": 1, "s": bad, "c": "politics"}]), response([{"i": 1, "s": bad, "c": "politics"}]),
        response([{"i": 1, "s": good, "c": "politics"}])]
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    review = AsyncMock(return_value={b.source_url: {"v": "pass"}})
    monkeypatch.setattr(summarizer, "assess_copy", review)
    out = await summarizer.summarize_articles([a, b], client=client)
    assert out[a.source_url]["_degraded"] is True
    assert out[b.source_url] == {"summary": good, "category": "politics"}
    assert [x["id"] for x in review.call_args.args[0]] == [b.source_url]
    assert client.messages.create.await_count == 5


@pytest.mark.asyncio
async def test_isolated_retries_keep_budget_blocked_articles_pending(monkeypatch):
    a, b = article(1), article(2)
    bad = "The agency filed charges against 49 people."
    rows = [{"i": 1, "s": bad, "c": "politics"}, {"i": 2, "s": bad, "c": "politics"}]
    client = AsyncMock()
    client.messages.create.side_effect = [response(rows), response(rows)]
    monkeypatch.setattr(summarizer, "check_budget", AsyncMock(side_effect=[SimpleNamespace(allowed=True),
        SimpleNamespace(allowed=False), SimpleNamespace(allowed=False)]))
    monkeypatch.setattr(summarizer, "assess_copy", AsyncMock(return_value={}))
    out = await summarizer.summarize_articles([a, b], client=client)
    assert all(out[x.source_url]["_degraded"] for x in [a, b])
    assert client.messages.create.await_count == 2
