"""store_node must not count the articles table on every cycle.

`pipeline_state.article_count` had no readers — the only consumers of that
table are app/main.py's startup seed (MAX(last_refreshed_at)) and sift's
per-category last_refreshed_at — yet keeping it current cost a
`SELECT COUNT(*) FROM articles WHERE category = $1 AND from_search = false`
per category, per cycle. No index serves that predicate exactly, the ten
categories partition the table, and the pipeline runs 48 times a day.

Nothing about reintroducing it would look wrong in review: a COUNT in a
bookkeeping UPSERT reads as ordinary. These tests are the thing that objects.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from workflows.pipeline_workflow import ALL_CATEGORIES, store_node

EMPTY_STATE = {
    "force": False,
    "articles": [],
    "new_articles": [],
    "summaries": {},
    "embeddings": {},
    "results": {},
    "total_skipped": 0,
    "errors": [],
}


def _all_sql(pool) -> list[str]:
    """Every SQL string this pool was asked to run, from any method."""
    out: list[str] = []
    for method in (pool.execute, pool.fetchval, pool.fetchrow, pool.fetch):
        for call in method.await_args_list:
            if call.args and isinstance(call.args[0], str):
                out.append(call.args[0])
    return out


async def run_store_node() -> list[str]:
    """Run store_node against a mock pool and return every SQL it issued."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=0)
    with patch("app.db.get_pool", new_callable=AsyncMock, return_value=pool):
        await store_node(dict(EMPTY_STATE))
    return _all_sql(pool)


class TestNoPerCycleCount:
    @pytest.mark.asyncio
    async def test_store_node_never_counts_the_whole_articles_table(self):
        """No COUNT over `articles` without a recency bound.

        The distinction is the point, and writing this test is what found it.
        The threading path also counts `articles` — its queue depth — but bounds
        the scan to `published_date > NOW() - INTERVAL '48 hours'`, so it reads
        a couple of thousand rows and does real work with the answer. The
        pipeline_state count had no bound at all, so it grew with the corpus
        forever, and fed a column nothing read.

        A count is fine. A count whose cost scales with total ingest, run 48
        times a day, is not.
        """
        executed_sql = await run_store_node()
        offenders = [
            s for s in executed_sql
            if "count(" in s.lower()
            and "from articles" in s.lower()
            and "interval" not in s.lower()
        ]
        assert not offenders, (
            "store_node issued an unbounded COUNT over `articles`. It runs 48x/day, "
            "so this walks the entire corpus every half hour. Bound it by recency, "
            f"or drop it if nothing reads the answer: {offenders}"
        )

    @pytest.mark.asyncio
    async def test_pipeline_state_is_written_in_one_statement(self):
        executed_sql = await run_store_node()
        writes = [s for s in executed_sql if "pipeline_state" in s.lower()]
        assert len(writes) == 1, (
            "pipeline_state should be updated by a single statement covering every "
            f"category, not one per category. Got {len(writes)}: {writes}"
        )

    @pytest.mark.asyncio
    async def test_that_one_statement_covers_every_category(self):
        executed_sql = await run_store_node()
        write = next(s for s in executed_sql if "pipeline_state" in s.lower())
        # unnest() over the category array is what makes it one round-trip; a
        # rewrite that loses it has almost certainly gone back to per-category.
        assert "unnest" in write.lower(), (
            f"expected a set-based write over ALL_CATEGORIES, got: {write}"
        )
        assert len(ALL_CATEGORIES) == 10
