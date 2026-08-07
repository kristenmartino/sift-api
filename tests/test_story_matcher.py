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
    NEAR_MISS_FLOOR,
    SHADOW_SAMPLE,
    RECENCY_WINDOW_HOURS,
    SIMILARITY_THRESHOLD,
    Candidate,
    fetch_queue,
    find_candidates,
    mark_threaded,
    shadow_report,
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


def _cand(article, existing=None, loose=None, near=None, outlets=1):
    return Candidate(article=article, existing_stories=existing or {},
                     loose_neighbours=loose or [], near_misses=near or [],
                     unique_outlets=outlets)


def test_summarize_counts_what_the_run_event_reports():
    cands = [
        _cand(_article("a1"), existing={"s": [{}]}),
        _cand(_article("a2"), loose=[{}], outlets=2),
        _cand(_article("a3")),
    ]
    s = summarize(cands)
    assert s["analysed"] == 3
    assert s["attach_candidates"] == 1
    assert s["new_cluster_candidates"] == 1
    assert s["parked"] == 1
    assert s["llm_relevant"] == 2


def test_outlet_gate_is_measured_for_free_before_any_llm_call():
    """A new cluster needs >= 2 outlets to survive. Counting that here is what
    turns a candidate count from an upper bound into something closer to a
    prediction — one of the two filters between candidates and grouping."""
    cands = [
        _cand(_article("a1"), loose=[{}], outlets=3),   # survives
        _cand(_article("a2"), loose=[{}], outlets=1),   # one outlet, dropped
        _cand(_article("a3"), existing={"s": [{}]}, outlets=1),  # attach, exempt
    ]
    s = summarize(cands)
    assert s["new_cluster_candidates"] == 2
    assert s["new_clusters_passing_outlet_gate"] == 1


def test_near_miss_only_articles_are_counted_separately():
    """If 0.60 is too strict this is where it shows. Without the band, a
    threshold set too high is indistinguishable from a corpus with nothing
    to find."""
    cands = [
        _cand(_article("a1"), near=[{"similarity": 0.55}]),
        _cand(_article("a2")),
    ]
    s = summarize(cands)
    assert s["parked"] == 2
    assert s["parked_with_near_miss"] == 1
    assert s["near_miss_floor"] == NEAR_MISS_FLOOR


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


# ── near-miss band and outlet diversity ─────────────────────


@pytest.mark.asyncio
async def test_near_misses_are_recorded_but_not_acted_on():
    pool = _pool([
        _neighbour(0.70, aid="strong"),
        _neighbour(0.55, aid="near"),
        _neighbour(0.40, aid="weak"),
    ])
    [c] = await find_candidates(pool, [_article()])
    assert [n["id"] for n in c["loose_neighbours"]] == ["strong"]
    assert [n["id"] for n in c["near_misses"]] == ["near"]


@pytest.mark.asyncio
async def test_scan_still_stops_below_the_near_miss_floor():
    """The early break moved down rather than away — everything under the
    floor is still skipped, so widening the band costs nothing."""
    pool = _pool([
        _neighbour(NEAR_MISS_FLOOR - 0.01, aid="below"),
        _neighbour(0.99, aid="unreachable"),
    ])
    [c] = await find_candidates(pool, [_article()])
    assert c["near_misses"] == [] and c["loose_neighbours"] == []


@pytest.mark.asyncio
async def test_unique_outlets_counts_the_article_and_its_strong_neighbours():
    pool = _pool([
        {**_neighbour(0.80, aid="n1"), "source_name": "AP"},
        {**_neighbour(0.75, aid="n2"), "source_name": "Reuters"},
        {**_neighbour(0.55, aid="n3"), "source_name": "BBC"},  # near miss, excluded
    ])
    article = {**_article(), "source_name": "AP"}
    [c] = await find_candidates(pool, [article])
    # AP (article) + AP (n1) + Reuters (n2) = 2 distinct; BBC is a near miss.
    assert c["unique_outlets"] == 2


# ── confirm dry run ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_shadow_without_confirm_makes_no_llm_call():
    pool = _pool([])
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=0)
    r = await shadow_report(pool)
    assert r == {"event": "incremental_threading_shadow", "backlog": 0, "sampled": 0}


@pytest.mark.asyncio
async def test_shadow_samples_newest_not_the_queue():
    """The queue is oldest-first and never drains while the flag is off, so
    measuring it re-decides one stale slice every run — at ~10x the cost, and
    biased favourably, because a 47h-old article sees 47h of articles
    published after it as neighbours."""
    pool = _pool([])
    pool.fetchval = AsyncMock(return_value=4400)
    pool.fetch = AsyncMock(return_value=[])
    r = await shadow_report(pool)

    sql = pool.fetch.call_args.args[0]
    assert "ORDER BY published_date DESC" in sql
    assert "threaded_at IS NULL" not in sql
    assert pool.fetch.call_args.args[1] == SHADOW_SAMPLE
    # Backlog is still reported, just not what the rates come from.
    assert r["backlog"] == 4400


@pytest.mark.asyncio
async def test_dry_run_reports_what_the_confirmer_decided_without_writing():
    """The one piece of cutover evidence that costs money. would_group is the
    number the bar actually compares against the live path's grouped count."""
    queue_row = {**_article("a1"), "image_url": None, "published_date": None,
                 "entities": {}}
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=1)
    pool.fetch = AsyncMock(side_effect=[
        [queue_row],                              # fetch_recent_sample
        [_neighbour(0.80, story_id="s1")],        # find_candidates
    ])
    pool.execute = AsyncMock()

    confirm = AsyncMock(return_value={
        "a1": {"action": "attach", "story_id": "s1", "members": []},
    })
    r = await shadow_report(pool, confirm=confirm)

    confirm.assert_awaited_once()
    assert r["dry_run"] == {"attach": 1, "new": 0, "none": 0}
    assert r["would_group"] == 1
    assert r["confirm_rate"] == 1.0
    # Nothing written, nothing marked.
    pool.execute.assert_not_called()


# ── shadow persistence ──────────────────────────────────────
#
# The shadow report gates the cutover and used to live only in the Railway log
# buffer, which rotates and resets on deploy. Same failure ai_usage_daily had:
# the number nobody could query was the number nobody checked.


@pytest.mark.asyncio
async def test_record_shadow_persists_the_report():
    from services.story_matcher import record_shadow

    pool = AsyncMock()
    await record_shadow(pool, {
        "backlog": 4367, "sampled": 40, "attach_candidates": 7,
        "new_cluster_candidates": 10, "new_clusters_passing_outlet_gate": 5,
        "parked": 23, "parked_with_near_miss": 11, "llm_relevant": 17,
        "threshold": 0.6, "near_miss_floor": 0.5,
        "llm_relevant_by_category": {"sports": 4},
        "dry_run": {"attach": 7, "new": 2, "none": 8},
        "would_group": 9, "confirm_rate": 0.529,
    })
    pool.execute.assert_awaited_once()
    assert "INSERT INTO threading_shadow" in pool.execute.call_args.args[0]
    assert pool.execute.call_args.args[1] == 4367   # backlog
    assert pool.execute.call_args.args[13] == 9     # would_group


@pytest.mark.asyncio
async def test_record_shadow_skips_an_empty_run():
    from services.story_matcher import record_shadow

    pool = AsyncMock()
    await record_shadow(pool, {"backlog": 0, "sampled": 0})
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_record_shadow_never_raises():
    """Losing a telemetry row must not break ingest, and the numbers were
    already logged before this ran."""
    from services.story_matcher import record_shadow

    pool = AsyncMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("table missing"))

    await record_shadow(pool, {"sampled": 40, "backlog": 1})

    # Assert it actually reached the write and swallowed a real failure —
    # without this the test would still pass if record_shadow short-circuited
    # before trying anything, which is the shape tests/test_meta_suite.py
    # exists to catch. Mirrors test_cost_guard's write_error_is_swallowed.
    pool.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_shadow_stores_null_dry_run_when_absent():
    from services.story_matcher import record_shadow

    pool = AsyncMock()
    await record_shadow(pool, {"sampled": 40, "backlog": 1})
    assert pool.execute.call_args.args[12] is None  # dry_run
    assert pool.execute.call_args.args[13] is None  # would_group
