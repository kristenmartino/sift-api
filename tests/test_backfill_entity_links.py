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

    async def _connect(*a, **kw):
        return conn

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(backfill.asyncpg, "connect", _connect)
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

    async def _connect(*a, **kw):
        return conn

    async def _create(**kwargs):
        return _mock_response("[]")

    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(backfill.asyncpg, "connect", _connect)
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
