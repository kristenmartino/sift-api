"""Tests for workflows.story_workflow.synthesize_and_store_node.

This module had no tests, and it owns the single most expensive decision in the
pipeline: whether to spend a Haiku call synthesizing a story.

Measured on prod over 2026-07-31..08-04, `story_synthesizer.synthesize` was
called 5,491 times to produce 2,500 stories — 54% of that spend regenerated
text for a story row that already existed. `story_id` is a sha256 of the sorted
member article ids, so an id collision means the *identical* member set; there
is nothing new to synthesize.

These cover the reuse decision and the one case that must NOT reuse: a row left
in `synthesis_status='failed'`, which holds a degraded placeholder (the first
article's raw title/summary) rather than a real synthesis.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def _pool(existing: dict | None) -> AsyncMock:
    """A pool whose `SELECT headline, synthesis_status` returns `existing`."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=existing)
    pool.execute = AsyncMock(return_value=None)
    return pool


def _state(n_outlets: int = 2) -> dict:
    articles = [
        {
            "id": f"id-{i}",
            "source_url": f"https://example.com/{i}",
            "source_name": f"Outlet{i if n_outlets > 1 else 1}",
            "title": f"Title {i}",
            "summary": f"Summary {i}",
            "image_url": f"https://example.com/{i}.jpg",
            "published_date": f"2026-08-0{i}T12:00:00+00:00",
            "existing_story_id": None,
            "entities": {},
        }
        for i in (1, 2)
    ]
    return {
        "category": "politics",
        "articles": articles,
        "entities": {a["source_url"]: {} for a in articles},
        "clusters": [{"article_indices": [1, 2], "event": "an event"}],
        "stories": [],
        "errors": [],
    }


def _sql_of(pool: AsyncMock) -> str:
    """All SQL passed to pool.execute, concatenated, for shape assertions."""
    return "\n".join(str(c.args[0]) for c in pool.execute.call_args_list if c.args)


async def _run(pool: AsyncMock, synth: AsyncMock, state: dict) -> dict:
    from workflows.story_workflow import synthesize_and_store_node

    with patch("app.db.get_pool", AsyncMock(return_value=pool)), \
            patch("services.story_synthesizer.synthesize_story", synth):
        return await synthesize_and_store_node(state)


@pytest.mark.asyncio
async def test_existing_complete_story_skips_the_paid_call():
    """The 54% case: same member set → same id → nothing to regenerate."""
    pool = _pool({"headline": "Stored headline", "synthesis_status": "complete"})
    synth = AsyncMock()

    result = await _run(pool, synth, _state())

    synth.assert_not_called()
    assert result["stories"][0]["reused"] is True
    # Keeps the stored headline rather than inventing a new one.
    assert result["stories"][0]["headline"] == "Stored headline"
    sql = _sql_of(pool)
    assert "INSERT INTO stories" not in sql
    assert "UPDATE stories SET updated_at" in sql
    # Members are still re-pointed — the node NULLs story_id for the window
    # before this loop, so skipping the re-point would orphan the story.
    assert "UPDATE articles SET story_id" in sql


@pytest.mark.asyncio
async def test_unseen_story_is_synthesized_and_inserted():
    pool = _pool(None)
    synth = AsyncMock(return_value={
        "headline": "Fresh headline", "summary": "s", "framings": [],
    })

    result = await _run(pool, synth, _state())

    synth.assert_awaited_once()
    assert result["stories"][0]["reused"] is False
    assert result["stories"][0]["headline"] == "Fresh headline"
    assert "INSERT INTO stories" in _sql_of(pool)


@pytest.mark.asyncio
async def test_failed_row_is_retried_not_reused():
    """A 'failed' row holds the first article's raw text, never a synthesis.

    Reusing it would make a one-off API blip permanent.
    """
    pool = _pool({"headline": "Title 1", "synthesis_status": "failed"})
    synth = AsyncMock(return_value={
        "headline": "Real headline", "summary": "s", "framings": [],
    })

    result = await _run(pool, synth, _state())

    synth.assert_awaited_once()
    assert result["stories"][0]["reused"] is False
    assert result["stories"][0]["headline"] == "Real headline"
    assert "INSERT INTO stories" in _sql_of(pool)


@pytest.mark.asyncio
async def test_synthesis_failure_still_stores_a_degraded_row():
    pool = _pool(None)
    synth = AsyncMock(side_effect=RuntimeError("API down"))

    result = await _run(pool, synth, _state())

    assert result["stories"][0]["status"] == "failed"
    # Falls back to the first member's own title, per the except branch.
    assert result["stories"][0]["headline"] == "Title 1"


@pytest.mark.asyncio
async def test_single_outlet_cluster_never_reaches_the_paid_call():
    """Regression guard on the >=2-unique-outlets gate, which sits *before*
    the reuse check — a dropped cluster must cost nothing at all."""
    pool = _pool(None)
    synth = AsyncMock()

    result = await _run(pool, synth, _state(n_outlets=1))

    synth.assert_not_called()
    pool.fetchrow.assert_not_called()
    assert result["stories"] == []
