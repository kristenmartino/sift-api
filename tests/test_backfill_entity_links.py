"""Tests for scripts/backfill_entity_links.py — the LLM-mode write guard.

What this pins, and why it exists: on 2026-08-05 a scoped
`--mode llm --only-canonical` run over 296 articles hit a wave of 8s
`entity_linker_llm` timeouts and cleared 218 rows to []. 34 of the emptied
rows still plainly named catalog entities (the-new-york-times, EXEC-TRUMP-DJ,
united-states-congress, ...). The cause was that link_text_llm answered [] for
both "no entities mentioned" and "the call failed", and LLM mode overwrites
rather than merging — so an outage was written to the DB as fact.

The regression test is `test_llm_timeout_leaves_stored_links_untouched`: the
timeout is a real one (LLM_TIMEOUT_SECONDS is shrunk and the fake client
hangs), so it exercises the whole chain — link_text_llm → link_articles_llm
→ the script's write loop — rather than asserting on a mocked return value.

DB-free: asyncpg.connect is replaced with a fake connection that answers the
script's queries from in-memory rows and records what it would have written.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import asyncpg
import pytest

from services import entity_linker_llm

# Load the script as a module without invoking its main(); scripts/ is not a
# package. Same pattern as tests/test_seed_entity_aliases.py.
SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "backfill_entity_links.py"
_spec = importlib.util.spec_from_file_location("backfill_entity_links", SCRIPT)
assert _spec is not None and _spec.loader is not None
backfill = importlib.util.module_from_spec(_spec)
sys.modules["backfill_entity_links"] = backfill
_spec.loader.exec_module(backfill)


STORED_LINKS = [
    {"type": "org", "canonical_id": "united-states-congress",
     "surface_form": "Congress"},
]

ARTICLES = [
    {
        "id": "a-timeout",
        "title": "Congress weighs the bill",
        "summary": "Lawmakers met on Tuesday.",
        "source_url": "https://example.com/1",
        "source_name": "Reuters",
        "el": json.dumps(STORED_LINKS, separators=(",", ":")),
    },
    {
        "id": "a-ok",
        "title": "Schumer speaks",
        "summary": "The Senate Majority Leader spoke today.",
        "source_url": "https://example.com/2",
        "source_name": "Reuters",
        "el": "[]",
    },
]

_ANSWER = ('[{"type":"politician","canonical_id":"S000148",'
           '"surface_form":"Schumer"}]')


class FakeConn:
    """Enough of asyncpg.Connection for this script. Dispatches on the SQL
    text; records writes instead of performing them."""

    def __init__(self, articles: list[dict]):
        self.articles = articles
        self.writes: list[tuple[str, str]] = []

    async def fetch(self, sql: str, *params):
        if "FROM outlet_profiles" in sql:
            return [{"slug": "reuters", "name": "Reuters"}]
        if "FROM politician_profiles" in sql:
            return [{"bioguide_id": "S000148", "name": "Chuck Schumer"}]
        if "FROM org_profiles" in sql:
            return [{"slug": "united-states-congress",
                     "name": "United States Congress"}]
        if "FROM bill_profiles" in sql:
            return []
        if "FROM entity_aliases" in sql:
            return []
        if "FROM source_name_aliases" in sql:
            return []
        if sql.strip().startswith("SELECT id FROM articles"):
            return [{"id": a["id"]} for a in self.articles]
        if "entity_links::text AS el" in sql:
            wanted = set(params[0])
            return [a for a in self.articles if a["id"] in wanted]
        raise AssertionError(f"unexpected query: {sql}")

    async def fetchval(self, sql: str, *params):
        return len(self.articles)

    async def executemany(self, sql: str, args):
        assert "UPDATE articles SET entity_links" in sql
        self.writes.extend(args)

    def transaction(self):
        class _Txn:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Txn()

    async def close(self):
        return None


class FakePool:
    """Enough of asyncpg.Pool for this script.

    The script moved from one long-lived connection to a pool (a full-corpus
    run outlives Neon's session timeout), so reads go straight to the pool and
    only the write transaction acquires. Delegates everything to one FakeConn
    so assertions on `.writes` still see a single ledger.
    """

    def __init__(self, conn: "FakeConn"):
        self.conn = conn

    async def fetch(self, sql: str, *params):
        return await self.conn.fetch(sql, *params)

    async def fetchval(self, sql: str, *params):
        return await self.conn.fetchval(sql, *params)

    def acquire(self):
        outer = self

        class _Acq:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()

    async def close(self):
        return None


def _mock_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=80,
    )
    return SimpleNamespace(content=[block], usage=usage)


@pytest.fixture
def fake_conn(monkeypatch):
    """Wire the script to an in-memory DB and a controllable Claude."""
    conn = FakeConn([dict(a) for a in ARTICLES])

    async def _create_pool(*a, **kw):
        return FakePool(conn)

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(backfill.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(entity_linker_llm, "log_usage", lambda *a, **kw: None)
    # A real timeout, not a mocked return value — but a 10ms one.
    monkeypatch.setattr(entity_linker_llm, "LLM_TIMEOUT_SECONDS", 0.01)

    async def _create(**kwargs):
        # The article whose title says "Congress" is the one we stall.
        if "Congress" in kwargs["messages"][0]["content"]:
            await asyncio.sleep(5)
        return _mock_response(_ANSWER)

    monkeypatch.setattr(
        entity_linker_llm, "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=_create)),
    )
    return conn


async def _run(mode: str = "llm", **kw) -> int:
    return await backfill.main(
        dry_run=False, include_empty=False, mode=mode,
        limit=None, chunk_size=500, assume_yes=True, **kw,
    )


@pytest.mark.asyncio
async def test_llm_timeout_leaves_stored_links_untouched(fake_conn):
    """The regression. A timed-out article is not written at all; the article
    that answered still is."""
    assert await _run() == 0

    written_ids = {aid for _, aid in fake_conn.writes}
    assert "a-timeout" not in written_ids, (
        "a timed-out LLM call was written back as an entity_links value"
    )
    assert written_ids == {"a-ok"}

    new_json = next(j for j, aid in fake_conn.writes if aid == "a-ok")
    assert json.loads(new_json)[0]["canonical_id"] == "S000148"


@pytest.mark.asyncio
async def test_llm_mode_still_clears_when_the_model_answers_empty(monkeypatch):
    """The guard must not cost LLM mode its purpose: clearing a bad chip the
    additive regex pass cannot remove. A real '[]' answer still overwrites."""
    conn = FakeConn([dict(ARTICLES[0])])

    async def _create_pool(*a, **kw):
        return FakePool(conn)

    async def _create(**kwargs):
        return _mock_response("[]")

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(backfill.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(entity_linker_llm, "log_usage", lambda *a, **kw: None)
    monkeypatch.setattr(
        entity_linker_llm, "_client",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=_create)),
    )

    assert await _run() == 0
    assert conn.writes == [("[]", "a-timeout")]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(fake_conn):
    assert await backfill.main(
        dry_run=True, include_empty=False, mode="llm",
        limit=None, chunk_size=500, assume_yes=True,
    ) == 0
    assert fake_conn.writes == []


# ── connection resilience ────────────────────────────────────────────────
#
# A full-corpus run holds the client open for ~an hour. On 2026-08-05 one died
# at 180,000/284,540 with ConnectionDoesNotExistError raised from inside
# `conn.transaction()` — Neon had closed the session. Nothing was corrupted
# (writes are per-chunk and committed), but the run had to be restarted from
# the top. These cover the retry that makes that survivable.

class FlakyPool(FakePool):
    """Raises a dropped-connection error the first time, then behaves."""

    def __init__(self, conn, fail_on: str):
        super().__init__(conn)
        self.fail_on = fail_on          # "read" | "write"
        self.raised = False

    async def fetch(self, sql: str, *params):
        if (self.fail_on == "read" and not self.raised
                and "entity_links::text AS el" in sql):
            self.raised = True
            raise asyncpg.exceptions.ConnectionDoesNotExistError(
                "connection was closed in the middle of operation")
        return await super().fetch(sql, *params)

    def acquire(self):
        if self.fail_on == "write" and not self.raised:
            self.raised = True

            class _Boom:
                async def __aenter__(self_inner):
                    raise asyncpg.exceptions.ConnectionDoesNotExistError(
                        "connection was closed in the middle of operation")

                async def __aexit__(self_inner, *exc):
                    return False

            return _Boom()
        return super().acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", ["read", "write"])
async def test_dropped_connection_is_retried_not_fatal(monkeypatch, fail_on):
    # Purpose-built rows: the shared ARTICLES fixture contains no full catalog
    # name, so a regex pass over it writes nothing and could not distinguish a
    # successful retry from a silently skipped chunk.
    conn = FakeConn([
        {
            "id": "will-link",
            "title": "United States Congress weighs the bill",
            "summary": "Chuck Schumer spoke on Tuesday.",
            "source_url": "https://example.com/1",
            "source_name": "Reuters",
            "el": "[]",
        },
    ])
    pool = FlakyPool(conn, fail_on)

    async def _create_pool(*a, **kw):
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(backfill.asyncpg, "create_pool", _create_pool)

    rc = await backfill.main(
        dry_run=False, include_empty=True, mode="regex",
        limit=None, chunk_size=1, assume_yes=True,
    )

    assert rc == 0, "a single dropped connection must not fail the run"
    assert pool.raised, "the test did not actually exercise the failure path"
    # The retried chunk must still be processed, not silently skipped. "Schumer
    # speaks" is stored as [] and the catalog holds Chuck Schumer, so the regex
    # pass has exactly one write to make — and it is in the chunk that failed.
    assert [w[1] for w in conn.writes] == ["will-link"], (
        f"expected the retried chunk to still write, got {conn.writes!r}"
    )
