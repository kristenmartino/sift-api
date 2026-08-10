"""Tests for --prune on the politician seeder.

DB-free: a fake pool answers the two queries `_retire_departed` runs and
records what it would write.

The case that matters most is the scoping one. The roster CSV holds current
Congress only — the executive, foreign-executive and scotus rows come from
other seeders and are absent from it *by design*. An unscoped prune would
read all 112 of them as "departed" and retire the entire executive branch,
including every row migrations 015 and 016 built.
"""
from __future__ import annotations

import pytest

from scripts.seed_politician_profiles import _retire_departed

SITTING = [
    {"bioguide_id": "S000148", "name": "Chuck Schumer", "state": "NY", "chamber": "senate"},
    {"bioguide_id": "M001153", "name": "Lisa Murkowski", "state": "AK", "chamber": "senate"},
]
# Present in the DB, never in the roster CSV. Must survive any prune.
NON_CONGRESS = [
    {"bioguide_id": "EXEC-TRUMP-DJ", "name": "Donald J. Trump", "state": "US", "chamber": "executive"},
    {"bioguide_id": "FOREIGN-PUTIN-V", "name": "Vladimir Putin", "state": "RU", "chamber": "foreign-executive"},
    {"bioguide_id": "SCOTUS-ROBERTS-J", "name": "John Roberts", "state": "US", "chamber": "scotus"},
    {"bioguide_id": "G000359", "name": "Lindsey Graham", "state": "SC", "chamber": "former"},
]


class FakePool:
    """Answers the SELECT `_retire_departed` runs; records UPDATEs."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params):
        assert "FROM politician_profiles" in sql
        # The scoping lives in SQL, so the fake has to honour it or the test
        # would pass on a query that selects everything.
        assert "chamber IN ('house', 'senate')" in sql, (
            "the departed query must be scoped to sitting Congress"
        )
        return [r for r in self.rows if r["chamber"] in ("house", "senate")]

    async def fetchval(self, sql: str, *params):
        return 0  # chip count; not under test here

    def acquire(self):
        outer = self

        class _Conn:
            async def execute(self_inner, sql, *params):
                outer.executed.append((sql, params))

            def transaction(self_inner):
                class _Txn:
                    async def __aenter__(s):
                        return None

                    async def __aexit__(s, *e):
                        return False

                return _Txn()

        class _Acq:
            async def __aenter__(self_inner):
                return _Conn()

            async def __aexit__(self_inner, *e):
                return False

        return _Acq()


@pytest.mark.asyncio
async def test_retires_a_departed_sitting_member():
    pool = FakePool(SITTING + NON_CONGRESS)
    # Schumer absent from the CSV => departed.
    await _retire_departed(pool, {"M001153"}, dry_run=False, prune=True)

    assert len(pool.executed) == 1
    sql, params = pool.executed[0]
    assert "chamber = 'former'" in sql
    assert params[0] == ["S000148"]


@pytest.mark.asyncio
async def test_never_retires_executive_foreign_or_scotus_rows():
    """The guard that stops a roster refresh deleting the executive branch.

    None of these ids are in the CSV, so an unscoped implementation would
    retire all four. Only the sitting member may be touched.
    """
    pool = FakePool(SITTING + NON_CONGRESS)
    await _retire_departed(pool, set(), dry_run=False, prune=True)

    assert len(pool.executed) == 1
    _, params = pool.executed[0]
    retired = set(params[0])
    assert retired == {"S000148", "M001153"}
    for row in NON_CONGRESS:
        assert row["bioguide_id"] not in retired


@pytest.mark.asyncio
async def test_does_nothing_without_prune():
    pool = FakePool(SITTING)
    await _retire_departed(pool, set(), dry_run=False, prune=False)
    assert pool.executed == []


@pytest.mark.asyncio
async def test_does_nothing_on_dry_run():
    pool = FakePool(SITTING)
    await _retire_departed(pool, set(), dry_run=True, prune=True)
    assert pool.executed == []


@pytest.mark.asyncio
async def test_no_write_when_every_sitting_row_is_in_the_csv():
    pool = FakePool(SITTING + NON_CONGRESS)
    await _retire_departed(
        pool, {"S000148", "M001153"}, dry_run=False, prune=True,
    )
    assert pool.executed == []


@pytest.mark.asyncio
async def test_updates_rather_than_deletes():
    """A politician row is referenced by articles.entity_links and carries
    LCV scores and curated notes this script does not own. Retiring keeps the
    dossier working and drops it from the sitemap; deleting would turn every
    stored chip into a link to a 404."""
    pool = FakePool(SITTING)
    await _retire_departed(pool, set(), dry_run=False, prune=True)

    sql, _ = pool.executed[0]
    assert sql.strip().upper().startswith("UPDATE")
    assert "DELETE" not in sql.upper()
