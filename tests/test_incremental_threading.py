"""Tests for the incremental threading write path.

The thing being fixed is story *identity*. story_workflow derives story_id
from a sha256 of its member ids, so gaining an article makes a story a
different story — 58,259 of 58,557 rows (99.5%) have no members. These pin
the property that replaces it: an id derived once from the seed and stable
under growth.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from workflows.incremental_threading import (
    MAX_SYNTHESIS_ATTEMPTS,
    MIN_UNIQUE_OUTLETS,
    SWEEP_LIMIT,
    _sweep_failed,
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


def _sweep_pool(targets, members=None, attempts_seen=None):
    """A pool that answers the sweeper's two queries and records writes.

    `targets` are the story ids `_sweep_failed` should be handed; `members` the
    articles each one currently holds.
    """
    members = members if members is not None else [_article("m1", "AP"),
                                                   _article("m2", "Reuters")]

    async def fetch(sql, *a):
        if "synthesis_attempts < $1" in sql:
            if attempts_seen is not None:
                attempts_seen.append(a)
            return [{"id": t} for t in targets]
        if "WHERE story_id = $1" in sql:
            return members
        return []

    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    pool.fetchval = AsyncMock(return_value=0)  # unrepairable count
    pool.execute = AsyncMock()
    return pool


# ── story identity ──────────────────────────────────────────


class TestSeedStoryId:
    def test_is_deterministic_so_a_repeated_seed_is_idempotent(self):
        assert seed_story_id("politics", ["b", "a"]) == seed_story_id("politics", ["a", "b"])

    def test_differs_by_category(self):
        assert seed_story_id("politics", ["a"]) != seed_story_id("sports", ["a"])

    def test_a_different_seed_is_a_different_story(self):
        """The id is a function of the seed, so two different seeds must not
        collide onto one story row."""
        assert seed_story_id("politics", ["a", "b", "c"]) != seed_story_id(
            "politics", ["a", "b"]
        )


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


# ── synthesis failure ───────────────────────────────────────
#
# `synthesize_story` degrades instead of raising: `_fallback()` returns the
# first member's own title and summary with no framings, flagged `_failed`.
# Storing that as 'complete' produced 4 prod stories (13-18 outlets each)
# serving one outlet's headline under "how N outlets covered this", with
# nothing to revisit them.


@pytest.mark.asyncio
async def test_a_failed_resynthesis_does_not_overwrite_the_stored_story():
    """The story already has a real synthesis. A failed refresh must take the
    new article's count and nothing else — one outlet's copy is worse than
    slightly stale framings, and 'complete' would make it permanent."""
    pool = _pool({
        "DISTINCT source_name": [{"source_name": "AP"}],
        "WHERE story_id = $1": [_article("a1", "AP"), _article("a2", "Reuters")],
    })
    synth = AsyncMock(return_value={
        "headline": "t a1", "summary": "s", "framings": [], "_failed": True,
    })
    cands = [{"article": _article("a2", "Reuters"),
              "existing_stories": {"s1": [{}]}, "loose_neighbours": []}]

    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a2": {"action": "attach", "story_id": "s1"}}),
        synthesize=synth,
    )

    sql = _sql(pool)
    assert "SET headline" not in sql          # existing text preserved
    assert "synthesis_status" not in sql      # and its status left alone
    assert "article_count" in sql
    assert r["attached"] == 1
    assert r["resynthesized"] == 0
    assert r["synthesis_failed"] == 1


@pytest.mark.asyncio
async def test_a_new_story_whose_synthesis_failed_is_stored_as_failed():
    """Mirrors story_workflow.py:246. 'failed' keeps the row out of the feed
    (idx_stories_feed) instead of publishing a fallback as a finished story."""
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")],
    })
    synth = AsyncMock(return_value={
        "headline": "t a1", "summary": "s", "framings": [], "_failed": True,
    })

    cands = [{"article": _article("a1", "Reuters"),
              "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]}]
    r = await run_incremental_threading(
        pool,
        candidates=cands,
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
        synthesize=synth,
    )

    assert r["created"] == 1
    assert r["synthesis_failed"] == 1
    insert = next(c for c in pool.execute.call_args_list
                  if c.args and "INSERT INTO stories" in str(c.args[0]))
    assert insert.args[-1] == "failed"


@pytest.mark.asyncio
async def test_a_successful_synthesis_is_still_stored_as_complete():
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")],
    })
    r = await run_incremental_threading(
        pool,
        candidates=[{"article": _article("a1", "Reuters"),
                     "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]}],
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
        synthesize=AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []}),
    )

    assert r["created"] == 1 and r["synthesis_failed"] == 0
    insert = next(c for c in pool.execute.call_args_list
                  if c.args and "INSERT INTO stories" in str(c.args[0]))
    assert insert.args[-1] == "complete"


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


@pytest.mark.asyncio
async def test_a_later_joiner_does_not_change_the_story_id():
    """THE fix, exercised rather than asserted about.

    Under the old scheme gaining a member produced a different id, a new row,
    and an orphan — 58,259 of them. Identity has to belong to the story, not to
    its current membership.

    The test that used to stand here called `seed_story_id` twice with the SAME
    argument list and asserted the two results matched. That is f(x) == f(x):
    true for every possible implementation of a pure function, and nothing ever
    joined. Verified 2026-08-17 that appending `str(len(member_ids))` to the
    hash key — restoring the exact 58k-orphan bug — left this file green.

    So this one grows a real story through `_attach` and checks the id survives.

    The expected id is a GOLDEN LITERAL, not `seed_story_id(...)` recomputed
    here. Recomputing it reintroduces the same tautology one level up: under a
    mutated hash both the fixture and the assertion shift together and the test
    stays green. Confirmed — the first draft of this test did exactly that and
    survived the membership-dependent mutation. Pinning the literal is the same
    doctrine as the golden hashes in tests/test_rss.py.
    """
    story_id = "384293b3b19d2851"  # sha256("politics|a1|n1")[:16]
    assert seed_story_id("politics", ["a1", "n1"]) == story_id, (
        "the seed id scheme changed; every existing story row is keyed on the old one"
    )

    # 1. Create the story from its seed: Reuters + AP.
    create_pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")],
    })
    created = await run_incremental_threading(
        create_pool,
        candidates=[{"article": _article("a1", "Reuters"),
                     "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]}],
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
        synthesize=AsyncMock(
            return_value={"headline": "H", "summary": "S", "framings": []}),
    )
    assert created["created"] == 1
    # The row is written under the seed-derived id, not some other id.
    create_params = [c.args[1:] for c in create_pool.execute.call_args_list if len(c.args) > 1]
    assert any(story_id in params for params in create_params), (
        "the created story is not keyed on its seed id"
    )

    # 2. A third outlet joins later. The story now holds three members.
    grow_pool = _pool({
        "DISTINCT source_name": [{"source_name": "Reuters"}, {"source_name": "AP"}],
        "WHERE story_id = $1": [_article("a1", "Reuters"), _article("n1", "AP"),
                                _article("n2", "BBC")],
    })
    grown = await run_incremental_threading(
        grow_pool,
        candidates=[{"article": _article("n2", "BBC"),
                     "existing_stories": {story_id: [{}]}, "loose_neighbours": []}],
        confirm=AsyncMock(
            return_value={"n2": {"action": "attach", "story_id": story_id}}),
        synthesize=AsyncMock(
            return_value={"headline": "H2", "summary": "S2", "framings": []}),
    )

    assert grown["attached"] == 1
    # No second story row: growth updates in place, it does not re-create.
    assert "INSERT INTO stories" not in _sql(grow_pool)
    # And every write still names the ORIGINAL id — the article is pointed at
    # it, and the story row updated under it.
    # Every 16-hex-char id appearing in any write must be the original one:
    # a second distinct id here IS the orphan bug (a new row, old members
    # stranded). `mark_threaded` also writes on this pool with a list of
    # article ids, hence matching on shape rather than on every parameter.
    written_ids = {
        param
        for c in grow_pool.execute.call_args_list
        for param in c.args[1:]
        if isinstance(param, str) and re.fullmatch(r"[0-9a-f]{16}", param)
    }
    assert written_ids == {story_id}, (
        f"growth wrote story ids {written_ids}, expected only {story_id}"
    )


# ── queue semantics ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_parked_articles_are_marked_threaded_too():
    """If a no-match article stayed queued it would be re-examined every run,
    rebuilding the O(window) cost this design exists to remove. It stays
    searchable as a neighbour either way.

    This test used to pass NO candidates, so `run_incremental_threading`
    returned at the empty-queue guard before `mark_threaded` was reachable at
    all — there were no parked articles and nothing was marked, and
    `assert r == {"queued": 0}` was true of the early return alone. It now
    parks a real article and asserts it gets marked.
    """
    # One relevant article the confirmer declines to place, and one parked
    # alongside it. Both must leave the queue.
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")],
    })
    r = await run_incremental_threading(
        pool,
        candidates=[
            {"article": _article("a1", "Reuters"),
             "existing_stories": {}, "loose_neighbours": [{"id": "n1"}]},
            {"article": _article("p1", "BBC"),
             "existing_stories": {}, "loose_neighbours": []},
        ],
        confirm=AsyncMock(return_value={"a1": {"action": "none"}}),
        synthesize=AsyncMock(),
        sweep=False,
    )

    assert r["analysed"] == 2 and r["parked"] == 1
    marked = _sql(pool)
    assert "threaded" in marked.lower(), "the queue was never marked"
    # The parked article (p1) is in the marked set, not just the relevant one.
    marked_ids = [c.args[1:] for c in pool.execute.call_args_list if len(c.args) > 1]
    flat = [i for params in marked_ids for p in params
            for i in (p if isinstance(p, list) else [p])]
    assert "p1" in flat, "the parked article was left queued and will be re-examined"


@pytest.mark.asyncio
async def test_an_empty_queue_still_sweeps_failed_stories():
    """A story owing a synthesis has nothing to do with whether new articles
    arrived, and the empty-queue path returns before the main pass — so the
    sweep has to be on it explicitly or the quietest runs never repair."""
    pool = _pool()
    r = await run_incremental_threading(
        pool, confirm=AsyncMock(return_value={}), synthesize=AsyncMock(),
    )
    assert r["queued"] == 0
    assert r["swept"] == 0  # nothing eligible, but the sweep ran


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


# ── member stealing within a run ────────────────────────────
#
# find_candidates snapshots the whole queue before any decision is applied, so
# one loose article is routinely offered to several candidates in the same run.
# Measured on the first live run 2026-08-10: 7 of 54 new stories lost members
# to a later create, three of them down to zero — the orphan mechanism this
# design exists to remove, via a different door.


@pytest.mark.asyncio
async def test_a_second_cluster_cannot_steal_the_first_ones_member():
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("shared", "AP")],
    })
    synth = AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []})
    # Both candidates were offered the same loose neighbour, "shared".
    cands = [
        {"article": _article("a1", "Reuters"), "existing_stories": {},
         "loose_neighbours": [{"id": "shared"}]},
        {"article": _article("a2", "BBC"), "existing_stories": {},
         "loose_neighbours": [{"id": "shared"}]},
    ]
    r = await run_incremental_threading(
        pool, candidates=cands, synthesize=synth,
        confirm=AsyncMock(return_value={
            "a1": {"action": "new", "members": ["shared"]},
            "a2": {"action": "new", "members": ["shared"]},
        }),
    )
    # First create wins; the second has nothing left and is not attempted.
    assert r["created"] == 1
    assert r["already_claimed"] == 1
    assert synth.await_count == 1


@pytest.mark.asyncio
async def test_an_article_already_claimed_as_a_member_is_not_processed_again():
    """a2 is pulled into a1's story as a member, and also has its own decision
    later in the same run. Acting on it would move it out again."""
    pool = _pool({
        "id = ANY($1::text[])": [_article("a1", "Reuters"), _article("a2", "AP")],
    })
    cands = [
        {"article": _article("a1", "Reuters"), "existing_stories": {},
         "loose_neighbours": [{"id": "a2"}]},
        {"article": _article("a2", "AP"), "existing_stories": {"s9": [{}]},
         "loose_neighbours": []},
    ]
    r = await run_incremental_threading(
        pool, candidates=cands,
        synthesize=AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []}),
        confirm=AsyncMock(return_value={
            "a1": {"action": "new", "members": ["a2"]},
            "a2": {"action": "attach", "story_id": "s9"},
        }),
    )
    assert r["created"] == 1
    assert r["attached"] == 0          # a2 was already claimed by a1's story
    assert r["already_claimed"] == 1


@pytest.mark.asyncio
async def test_create_only_claims_members_still_unattached_in_the_database():
    """The authoritative guard. The in-memory set covers this run; the SQL
    covers anything that attached between snapshot and write."""
    pool = _pool({"id = ANY($1::text[])": [_article("a1", "Reuters"), _article("n1", "AP")]})
    await run_incremental_threading(
        pool,
        candidates=[{"article": _article("a1", "Reuters"), "existing_stories": {},
                     "loose_neighbours": [{"id": "n1"}]}],
        synthesize=AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []}),
        confirm=AsyncMock(return_value={"a1": {"action": "new", "members": ["n1"]}}),
    )
    fetched = [str(c.args[0]) for c in pool.fetch.call_args_list if c.args]
    member_query = next(q for q in fetched if "id = ANY($1::text[])" in q)
    assert "story_id IS NULL" in member_query


# ── the failed-story sweeper ────────────────────────────────
#
# `synthesis_status='failed'` means a story holds a degraded placeholder and
# still owes a real synthesis. Nothing acted on it: the only reader was
# story_workflow.py:226, which never runs while incremental threading is
# enabled (pipeline_workflow.py:459). #210 then made `_create` write exactly
# such rows, so the gap stopped being theoretical — a two-outlet story whose
# synthesis failed and which never gained a third outlet stayed dark forever.


@pytest.mark.asyncio
async def test_a_repaired_story_is_written_complete_and_leaves_the_population():
    pool = _sweep_pool(["s1"])
    synth = AsyncMock(return_value={
        "headline": "Real headline", "summary": "S",
        "framings": [{"source_name": "AP", "framing": "f", "tone": "neutral"}],
    })

    counts = await _sweep_failed(pool, synth)

    assert counts == {"swept": 1, "repaired": 1, "still_failed": 0, "unrepairable": 0}
    sql = _sql(pool)
    assert "synthesis_status = 'complete'" in sql
    assert "synthesis_attempts = synthesis_attempts + 1" in sql


@pytest.mark.asyncio
async def test_a_retry_that_fails_again_burns_an_attempt_and_writes_nothing_else():
    """The fallback must not be stored — same rule as `_attach`. And
    `updated_at` stays put: a failed retry is not activity, and bumping it
    would make a story nobody can see look fresh to every recency query."""
    pool = _sweep_pool(["s1"])
    synth = AsyncMock(return_value={
        "headline": "t m1", "summary": "s", "framings": [], "_failed": True,
    })

    counts = await _sweep_failed(pool, synth)

    assert counts["still_failed"] == 1 and counts["repaired"] == 0
    sql = _sql(pool)
    assert "synthesis_attempts = synthesis_attempts + 1" in sql
    assert "SET headline" not in sql
    assert "synthesis_status" not in sql
    assert "updated_at" not in sql


@pytest.mark.asyncio
async def test_retries_are_bounded_so_a_structural_failure_is_not_paid_for_forever():
    """A story failing for a structural reason would otherwise be re-synthesized
    every 30 minutes indefinitely.

    The bounds are asserted as LITERALS on purpose. This test used to compare
    `seen[0][0] == MAX_SYNTHESIS_ATTEMPTS` — the same module constant
    `_sweep_failed` binds as its query parameter — so it pinned argument
    position and nothing else. Verified 2026-08-17: with
    MAX_SYNTHESIS_ATTEMPTS = 10_000 and SWEEP_LIMIT = 5000, this file stayed
    green while every failed story would be re-synthesized ~forever at
    $0.0022 a call, every 30 minutes. Which is the thing the test is named for.
    """
    seen = []
    await _sweep_failed(_sweep_pool([], attempts_seen=seen), AsyncMock())
    assert seen, "the sweeper did not bound its selection"
    assert seen[0][0] == 3, "three retries, then the story is left alone"
    assert seen[0][2] == 5, "five per run keeps the sweep a rounding error"
    # And the constants are what the query actually binds — this half is the
    # wiring check the literals above cannot make on their own.
    assert (seen[0][0], seen[0][2]) == (MAX_SYNTHESIS_ATTEMPTS, SWEEP_LIMIT)


@pytest.mark.asyncio
async def test_only_multi_outlet_stories_are_retried():
    """Below MIN_UNIQUE_OUTLETS there is nothing to synthesize across, and
    `synthesize_story` would return `_fallback()` without even calling out."""
    seen = []
    await _sweep_failed(_sweep_pool([], attempts_seen=seen), AsyncMock())
    assert seen[0][1] == 2, "a story needs two outlets before there is anything to compare"
    assert seen[0][1] == MIN_UNIQUE_OUTLETS


@pytest.mark.asyncio
async def test_unrepairable_rows_are_counted_but_never_deleted():
    """Orphans and single-outlet rows cannot be fixed by re-asking. Deletion is
    destructive and already has an audited tool with archive-before-delete
    (`scripts/prune_orphan_stories.py`); an automatic pipeline path is the
    wrong place for it."""
    pool = _sweep_pool([])
    pool.fetchval = AsyncMock(return_value=7)

    counts = await _sweep_failed(pool, AsyncMock())

    assert counts["unrepairable"] == 7
    assert "DELETE" not in _sql(pool).upper()


@pytest.mark.asyncio
async def test_one_bad_story_does_not_stall_the_sweep():
    pool = _sweep_pool(["s1", "s2"])
    synth = AsyncMock(side_effect=[
        RuntimeError("api blip"),
        {"headline": "H", "summary": "S", "framings": []},
    ])

    counts = await _sweep_failed(pool, synth)

    assert counts["repaired"] == 1 and counts["still_failed"] == 1


@pytest.mark.asyncio
async def test_the_sweep_runs_after_the_main_pass_and_lands_in_the_report():
    """Repair is strictly lower priority than getting new articles threaded."""
    pool = _pool({"DISTINCT source_name": [{"source_name": "AP"}],
                  "WHERE story_id = $1": [_article("a1", "AP"), _article("a2", "Reuters")]})
    r = await run_incremental_threading(
        pool,
        candidates=[{"article": _article("a2", "Reuters"),
                     "existing_stories": {"s1": [{}]}, "loose_neighbours": []}],
        confirm=AsyncMock(return_value={"a2": {"action": "attach", "story_id": "s1"}}),
        synthesize=AsyncMock(return_value={"headline": "H", "summary": "S", "framings": []}),
    )
    assert r["attached"] == 1
    assert "swept" in r and "unrepairable" in r
