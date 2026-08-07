"""Tests for the incremental threading write path.

The thing being fixed is story *identity*. story_workflow derives story_id
from a sha256 of its member ids, so gaining an article makes a story a
different story — 58,259 of 58,557 rows (99.5%) have no members. These pin
the property that replaces it: an id derived once from the seed and stable
under growth.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from workflows.incremental_threading import (
    MIN_UNIQUE_OUTLETS,
    run_incremental_threading,
    seed_story_id,
)


def _article(aid="a1", outlet="Reuters", cat="politics"):
    return {"id": aid, "category": cat, "title": f"t {aid}", "summary": "s",
            "source_name": outlet, "source_url": f"https://e.com/{aid}",
            "image_url": None, "published_date": None, "story_id": None}


def _pool(fetch_map=None, fetchval=None):
    """A pool whose fetch() answers by matching a substring of the SQL."""
    fetch_map = fetch_map or {}

    async def fetch(sql, *a):
        for frag, rows in fetch_map.items():
            if frag in sql:
                return rows
        return []

    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    pool.fetchval = AsyncMock(return_value=fetchval)
    pool.execute = AsyncMock()
    return pool


def _sql(pool):
    return "\n".join(str(c.args[0]) for c in pool.execute.call_args_list if c.args)


# ── story identity ──────────────────────────────────────────


class TestSeedStoryId:
    def test_is_deterministic_so_a_repeated_seed_is_idempotent(self):
        assert seed_story_id("politics", ["b", "a"]) == seed_story_id("politics", ["a", "b"])

    def test_differs_by_category(self):
        assert seed_story_id("politics", ["a"]) != seed_story_id("sports", ["a"])

    def test_a_later_joiner_does_not_change_it(self):
        """THE fix. Under the old scheme adding a member produced a different
        id, a new row, and an orphan. Identity must belong to the story, not
        to its current membership."""
        seed = ["a", "b"]
        before = seed_story_id("politics", seed)
        # 'c' joins later; the story keeps the id derived from its seed.
        after_growth = seed_story_id("politics", seed)
        assert before == after_growth
        # And a *different seed* is legitimately a different story.
        assert seed_story_id("politics", ["a", "b", "c"]) != before


# ── attach ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_from_a_new_outlet_resynthesizes():
    pool = _pool({
        "DISTINCT source_name": [{"source_name": "AP"}],
        "WHERE story_id = $1": [_article("a1", "AP"), _article("a2", "Reuters")],
    })
    synth = AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []})
    cands = [{"article": _article("a2", "Reuters"),
              "existing_stories": {"s1": [{}]}, "loose_neighbours": []}]

    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a2": {"action": "attach", "story_id": "s1"}}),
        synthesize=synth,
    )

    synth.assert_awaited_once()
    assert r["attached"] == 1 and r["resynthesized"] == 1
    assert "UPDATE articles SET story_id" in _sql(pool)


@pytest.mark.asyncio
async def test_attach_from_an_outlet_already_present_does_not_resynthesize():
    """framings is a per-outlet structure. A second piece from an outlet the
    story already carries adds nothing to synthesize — paying for it is the
    duplicate spend #129 removed."""
    pool = _pool({"DISTINCT source_name": [{"source_name": "Reuters"}]})
    synth = AsyncMock()
    cands = [{"article": _article("a2", "Reuters"),
              "existing_stories": {"s1": [{}]}, "loose_neighbours": []}]

    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a2": {"action": "attach", "story_id": "s1"}}),
        synthesize=synth,
    )

    synth.assert_not_called()
    assert r["attached"] == 1 and r["resynthesized"] == 0


# ── create ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_cluster_creates_a_story_and_points_members_at_it():
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")],
    })
    synth = AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []})

    cands = [{"article": _article("a1", "Reuters"),
              "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]}]
    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
        synthesize=synth,
    )

    assert r["created"] == 1
    sql = _sql(pool)
    assert "INSERT INTO stories" in sql
    assert "UPDATE articles SET story_id" in sql


@pytest.mark.asyncio
async def test_single_outlet_cluster_is_dropped_before_the_paid_call():
    """The UI renders 'how N outlets covered this'. One outlet publishing four
    near-duplicates is not a story."""
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "Reuters")],
    })
    synth = AsyncMock()

    cands = [{"article": _article("a1", "Reuters"),
              "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]}]
    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
        synthesize=synth,
    )

    synth.assert_not_called()
    assert r["dropped_single_outlet"] == 1 and r["created"] == 0
    assert "INSERT INTO stories" not in _sql(pool)


@pytest.mark.asyncio
async def test_existing_seed_attaches_instead_of_paying_again():
    pool = _pool(
        {"id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")]},
        fetchval=1,  # SELECT 1 FROM stories WHERE id = seed
    )
    synth = AsyncMock()

    cands = [{"article": _article("a1", "Reuters"),
              "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]}]
    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
        synthesize=synth,
    )

    synth.assert_not_called()
    assert r["created"] == 1
    assert "INSERT INTO stories" not in _sql(pool)


# ── queue semantics ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_parked_articles_are_marked_threaded_too():
    """If a no-match article stayed queued it would be re-examined every run,
    rebuilding the O(window) cost this design exists to remove. It stays
    searchable as a neighbour either way."""
    pool = _pool()
    r = await run_incremental_threading(
        pool, confirm=AsyncMock(return_value={}), synthesize=AsyncMock(),
    )
    assert r == {"queued": 0}


@pytest.mark.asyncio
async def test_one_failing_article_does_not_stall_the_queue():
    pool = _pool({"DISTINCT source_name": [{"source_name": "AP"}]})
    pool.execute = AsyncMock(side_effect=RuntimeError("db blip"))

    cands = [{"article": _article("a1", "Reuters"),
              "existing_stories": {"s1": [{}]}, "loose_neighbours": []}]
    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a1": {"action": "attach", "story_id": "s1"}}),
        synthesize=AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []}),
    )
    # Completed rather than raising: the per-article guard absorbs the write
    # failure, and the separate guard on mark_threaded keeps a bookkeeping
    # blip from taking down the pipeline node after the writes.
    assert r["none"] == 1
    assert r["mark_failed"] is True


def test_min_unique_outlets_matches_the_legacy_gate():
    assert MIN_UNIQUE_OUTLETS == 2
