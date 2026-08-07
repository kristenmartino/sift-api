"""Tests for services.story_matcher — the free candidate step.

The expensive half of threading is the LLM call; this module decides how much
of it is needed. Getting the routing wrong is silent: too strict and events
stop grouping, too loose and the prompt fills with noise. These pin the
routing, the threshold boundary, and the queue semantics that keep an article
whose entities have not landed from being dropped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.story_matcher import (
    RECENCY_WINDOW_HOURS,
    SIMILARITY_THRESHOLD,
    Candidate,
    fetch_queue,
    find_candidates,
    mark_threaded,
    summarize,
)


def _article(aid="a1", category="politics"):
    return {"id": aid, "category": category, "title": f"title {aid}",
            "summary": "s", "source_url": f"https://e.com/{aid}",
            "source_name": "Outlet", "story_id": None}


def _neighbour(sim, *, story_id=None, aid="n1"):
    return {"id": aid, "title": f"neighbour {aid}", "summary": "s",
            "source_url": f"https://e.com/{aid}", "source_name": "Other",
            "story_id": story_id, "published_date": None, "similarity": sim}


def _pool(neighbour_rows):
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=neighbour_rows)
    pool.execute = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_neighbour_already_in_a_story_becomes_an_attach_candidate():
    """Attaching preserves the story's existing id. That is what stops the
    orphan churn — 99.5% of stories rows have no members because the
    content-addressed id mints a new row on any membership change."""
    pool = _pool([_neighbour(0.82, story_id="story-abc")])
    [c] = await find_candidates(pool, [_article()])
    assert c["existing_stories"] == {"story-abc": [_neighbour(0.82, story_id="story-abc")]}
    assert c["loose_neighbours"] == []


@pytest.mark.asyncio
async def test_unstoried_neighbour_becomes_a_new_cluster_candidate():
    pool = _pool([_neighbour(0.75)])
    [c] = await find_candidates(pool, [_article()])
    assert c["existing_stories"] == {}
    assert [n["similarity"] for n in c["loose_neighbours"]] == [0.75]


@pytest.mark.asyncio
async def test_below_threshold_parks_for_free():
    """The 55% of articles that match nothing must cost zero — no LLM call and
    no follow-up. They stay searchable as neighbours regardless."""
    pool = _pool([_neighbour(SIMILARITY_THRESHOLD - 0.01)])
    [c] = await find_candidates(pool, [_article()])
    assert c["existing_stories"] == {} and c["loose_neighbours"] == []


@pytest.mark.asyncio
async def test_threshold_is_inclusive():
    pool = _pool([_neighbour(SIMILARITY_THRESHOLD)])
    [c] = await find_candidates(pool, [_article()])
    assert len(c["loose_neighbours"]) == 1


@pytest.mark.asyncio
async def test_scan_stops_at_the_first_weak_neighbour():
    """Rows arrive ordered by distance, so the first sub-threshold row means
    every later one is worse. Without the break a long tail of noise would
    reach the prompt."""
    pool = _pool([
        _neighbour(0.90, aid="n1"),
        _neighbour(0.30, aid="n2"),
        _neighbour(0.95, aid="n3"),  # unreachable in a correctly ordered result
    ])
    [c] = await find_candidates(pool, [_article()])
    assert [n["id"] for n in c["loose_neighbours"]] == ["n1"]


@pytest.mark.asyncio
async def test_multiple_neighbours_group_under_their_own_stories():
    pool = _pool([
        _neighbour(0.88, story_id="s1", aid="n1"),
        _neighbour(0.80, story_id="s1", aid="n2"),
        _neighbour(0.72, story_id="s2", aid="n3"),
        _neighbour(0.65, aid="n4"),
    ])
    [c] = await find_candidates(pool, [_article()])
    assert sorted(c["existing_stories"]) == ["s1", "s2"]
    assert len(c["existing_stories"]["s1"]) == 2
    assert [n["id"] for n in c["loose_neighbours"]] == ["n4"]


def test_summarize_counts_what_the_run_event_reports():
    cands = [
        Candidate(article=_article("a1"), existing_stories={"s": [{}]}, loose_neighbours=[]),
        Candidate(article=_article("a2"), existing_stories={}, loose_neighbours=[{}]),
        Candidate(article=_article("a3"), existing_stories={}, loose_neighbours=[]),
    ]
    assert summarize(cands) == {
        "queued": 3, "attach_candidates": 1, "new_cluster_candidates": 1,
        "parked": 1, "llm_relevant": 2,
    }


@pytest.mark.asyncio
async def test_queue_is_bounded_by_the_window_so_historical_rows_never_enter():
    """~280k existing rows have threaded_at NULL and were deliberately not
    backfilled. The window bound is the only thing keeping them out of the
    queue, so it must be in the SQL."""
    pool = _pool([])
    await fetch_queue(pool)
    sql = pool.fetch.call_args.args[0]
    assert "threaded_at IS NULL" in sql
    assert f"INTERVAL '{RECENCY_WINDOW_HOURS} hours'" in sql
    # The entities filter is what makes a per-row marker necessary rather than
    # a watermark: unprocessed entities mean "not yet", not "skip forever".
    assert "jsonb_typeof(entities) = 'object'" in sql
    assert "embedding IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_mark_threaded_is_a_noop_on_an_empty_run():
    pool = _pool([])
    await mark_threaded(pool, [])
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_mark_threaded_marks_parked_articles_too():
    """A parked singleton must not be re-queued every run — that would rebuild
    the O(window) cost this design exists to remove."""
    pool = _pool([])
    await mark_threaded(pool, ["a1", "a2"])
    pool.execute.assert_awaited_once()
    assert pool.execute.call_args.args[1] == ["a1", "a2"]
