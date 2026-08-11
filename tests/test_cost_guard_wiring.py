"""What each paid stage does when the daily cost ceiling blocks it.

`tests/test_cost_guard.py` covers `check_budget` itself. This file covers the
call sites, because the interesting part is not that they stop — it is what
they leave behind when they do.

The guard is fail-closed and defaults off, so every one of these paths is
invisible in a normal test run: `check_budget` returns `allowed=True` without
touching the ledger. These tests force the blocked branch directly.

THE RULE THEY ENFORCE: a budget stop must be RECOVERABLE. It happens to a whole
UTC day at once, so any stage that writes a degraded value the pipeline will
never revisit turns one cheap day into permanent data loss. Each assertion
below is about that, not about the stop.
"""
from __future__ import annotations

import pytest

from app.models import RSSArticle
from services import entity_linker_llm, summarizer
from services.cost_guard import BudgetDecision


BLOCKED = BudgetDecision(False, "budget_exceeded", 99.0, 10.0)


def _blocked(*_a, **_kw):
    async def _inner(*_args, **_kwargs):
        return BLOCKED
    return _inner()


CATALOG = [{
    "type": "politician", "canonical_id": "S000148",
    "primary_name": "Chuck Schumer", "aliases": [],
}]


@pytest.mark.asyncio
async def test_summarizer_falls_back_to_raw_content_not_empty(monkeypatch):
    """The subtle one, and the reason the guard is not just `return {}`.

    `store_node` iterates `new_articles`, not `summaries`, so an article
    missing from this dict is still stored — with no summary. And
    `services.deduplicator` drops known source_urls on every later cycle, so it
    is never re-summarized. Returning {} would permanently blank every article
    of a budget-stopped day.
    """
    monkeypatch.setattr(summarizer, "check_budget", _blocked)
    articles = [
        RSSArticle(
            title="Congress weighs the bill",
            source_url="https://example.com/1",
            source_name="Reuters",
            raw_content="Lawmakers met on Tuesday to consider the measure " * 5,
            category="top",
        ),
    ]

    out = await summarizer.summarize_articles(articles)

    assert out, "a budget stop must not leave articles with no summary at all"
    assert out["https://example.com/1"]["summary"].startswith("Lawmakers met")
    assert out["https://example.com/1"]["category"] == summarizer.FALLBACK_CATEGORY


@pytest.mark.asyncio
async def test_summarizer_makes_no_paid_call_when_blocked(monkeypatch):
    """The stop has to happen before the money, not after."""
    monkeypatch.setattr(summarizer, "check_budget", _blocked)

    async def _boom(*_a, **_kw):
        raise AssertionError("paid summarizer call made while over budget")

    monkeypatch.setattr(summarizer, "_summarize_batch_with_retry", _boom)
    articles = [RSSArticle(
        title="t", source_url="u1", source_name="Reuters",
        raw_content="body text here " * 10, category="top",
    )]

    assert await summarizer.summarize_articles(articles)


@pytest.mark.asyncio
async def test_linker_omits_rather_than_clearing_when_blocked(monkeypatch):
    """Under omit_failures the linker must leave articles OUT, not map them
    to [].

    Same failure this flag was introduced for (#136): the write path in
    `backfill_entity_links.py` overwrites rather than merges, so `[]` from a
    budget stop would erase real entity_links. Leaving them out routes them to
    the regex fallback instead.
    """
    monkeypatch.setattr(entity_linker_llm, "check_budget", _blocked)
    articles = [{"source_url": "u1", "title": "Schumer speaks", "summary": "s"}]

    assert await entity_linker_llm.link_articles_llm(
        articles, CATALOG, omit_failures=True,
    ) == {}


@pytest.mark.asyncio
async def test_linker_stays_lenient_for_the_read_path_when_blocked(monkeypatch):
    """The read path only needs *an* answer per article; [] is right there,
    and the caller's regex fallback fills the gap."""
    monkeypatch.setattr(entity_linker_llm, "check_budget", _blocked)
    articles = [{"source_url": "u1", "title": "Schumer speaks", "summary": "s"}]

    assert await entity_linker_llm.link_articles_llm(articles, CATALOG) == {"u1": []}


@pytest.mark.asyncio
async def test_linker_makes_no_paid_call_when_blocked(monkeypatch):
    monkeypatch.setattr(entity_linker_llm, "check_budget", _blocked)

    def _boom():
        raise AssertionError("Anthropic client constructed while over budget")

    monkeypatch.setattr(entity_linker_llm, "_client", _boom)
    out = await entity_linker_llm.link_articles_llm(
        [{"source_url": "u1", "title": "t", "summary": "s"}], CATALOG,
    )
    # _boom would have raised had the guard not short-circuited; the returned
    # value proves it took the blocked branch rather than erroring past it.
    assert out == {"u1": []}


@pytest.mark.asyncio
async def test_incremental_threading_leaves_the_queue_unmarked(monkeypatch):
    """Threading must skip WHOLE, leaving nothing marked threaded.

    `_attach` and `_create` write synthesis_status='complete' unconditionally,
    so a story synthesized from `story_synthesizer._fallback()` would be stored
    as finished and never revisited. Skipping before `confirm` means the
    articles stay queued and the next run redoes the work properly.
    """
    from workflows import incremental_threading as it

    monkeypatch.setattr(it, "check_budget", _blocked)

    async def _boom(*_a, **_kw):
        raise AssertionError("paid threading call made while over budget")

    candidates = [{
        "article": {"id": "a1", "source_name": "Reuters"},
        "existing_stories": [{"story_id": "s1"}],
        "loose_neighbours": [],
    }]

    out = await it.run_incremental_threading(
        pool=None, candidates=candidates, confirm=_boom, synthesize=_boom,
    )

    assert out.get("skipped") == "budget_exceeded"
    assert out["queued"] == 1
