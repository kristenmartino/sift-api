"""Real PostgreSQL concurrency tests. URL must point at an isolated local test DB."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from services import cost_guard


@pytest_asyncio.fixture
async def budget_db(monkeypatch):
    url = os.environ.get("AI_BUDGET_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated budget test database not configured")
    assert urlparse(url).hostname in {"localhost", "127.0.0.1"}
    schema = "budget_test_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, min_size=1, max_size=12, server_settings={"search_path": schema})
    await pool.execute("""
      CREATE TABLE api_batches(batch_id TEXT PRIMARY KEY);
      CREATE TABLE ai_usage_daily (
        usage_date DATE, provider TEXT, model TEXT, operation TEXT,
        estimated_cost_usd DOUBLE PRECISION DEFAULT 0, call_count INT DEFAULT 0,
        input_tokens BIGINT DEFAULT 0, output_tokens BIGINT DEFAULT 0,
        cache_read_tokens BIGINT DEFAULT 0, cache_write_tokens BIGINT DEFAULT 0,
        web_search_calls BIGINT DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(usage_date, provider, model, operation));
    """)
    await pool.execute((Path(__file__).parent.parent / "migrations/036_compare_budget.sql").read_text())
    await pool.execute("INSERT INTO ai_budget_policy VALUES(TRUE, TRUE, 15, 0.2)")

    async def get_pool():
        return pool

    monkeypatch.setattr(cost_guard, "get_pool", get_pool)
    monkeypatch.setattr(cost_guard.settings, "ai_cost_guard_enabled", True)
    yield pool
    await pool.close()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


@pytest.mark.asyncio
async def test_concurrent_background_cannot_spend_protected_compare_allocation(budget_db):
    holds = await asyncio.gather(*(cost_guard.reserve_budget(3) for _ in range(10)))
    assert sum(h is not None for h in holds) == 4
    interactive = await asyncio.gather(*(cost_guard.reserve_budget(1.5, scope="compare") for _ in range(3)))
    assert sum(h is not None for h in interactive) == 2
    snapshot = await cost_guard.budget_snapshot()
    assert snapshot["held_usd"] == 15
    assert snapshot["daily_limit_usd"] == 15


@pytest.mark.asyncio
async def test_settlement_is_atomic_idempotent_and_distinguishes_interactive(budget_db):
    await budget_db.execute(
        "INSERT INTO ai_usage_daily(usage_date, provider, model, operation, estimated_cost_usd) VALUES((NOW() AT TIME ZONE 'UTC')::date,'test','test','background',11.9)"
    )
    assert await cost_guard.reserve_budget(0.2) is None
    hold = await cost_guard.reserve_budget(0.5, scope="compare")
    assert hold
    assert await cost_guard.record_usage(
        "anthropic", "test", "compare.search_sources", 0.05, reservation_id=hold
    )
    assert await cost_guard.record_usage(
        "anthropic", "test", "compare.search_sources", 0.05, reservation_id=hold
    )
    row = await budget_db.fetchrow(
        "SELECT * FROM ai_usage_daily WHERE operation = 'interactive.compare.search_sources'"
    )
    assert row["call_count"] == 1
    assert row["estimated_cost_usd"] == pytest.approx(0.05)
    assert (await cost_guard.budget_snapshot())["held_usd"] == 0


@pytest.mark.asyncio
async def test_unresolved_prior_day_batch_cost_still_counts(budget_db):
    await budget_db.execute(
        "INSERT INTO ai_budget_holds(id, scope, remaining_usd, created_at) VALUES('yesterday-batch', 'background', 3, NOW() - INTERVAL '1 day')"
    )
    await budget_db.execute(
        "INSERT INTO ai_usage_daily(usage_date, provider, model, operation, estimated_cost_usd) VALUES((NOW() AT TIME ZONE 'UTC')::date,'test','test','background',10)"
    )
    assert await cost_guard.reserve_budget(0.01) is None
    assert await cost_guard.reserve_budget(2, scope="compare")
    assert await cost_guard.reserve_budget(0.01, scope="compare") is None


@pytest.mark.asyncio
async def test_zero_background_allocation_and_unreadable_policy_fail_closed(budget_db):
    await budget_db.execute("UPDATE ai_budget_policy SET compare_share = 1")
    assert await cost_guard.reserve_budget(0.01) is None
    assert await cost_guard.reserve_budget(0.01, scope="compare")
    await budget_db.execute("DELETE FROM ai_budget_policy")
    assert await cost_guard.reserve_budget(0.01, scope="compare") is None


@pytest.mark.asyncio
async def test_five_outlet_compare_reuses_capacity_when_only_80_cents_remain(budget_db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from workflows import compare_workflow as workflow
    from services.paid_calls import budget_scope

    await budget_db.execute("INSERT INTO ai_usage_daily(usage_date, provider, model, operation, estimated_cost_usd) VALUES((NOW() AT TIME ZONE 'UTC')::date,'test','test','background',14.2)")
    active = peak = 0
    async def create(**_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=10_000, output_tokens=100), content=[
            SimpleNamespace(type="text", text="The outlet reports the event."),
            SimpleNamespace(type="server_tool_use", name="web_search"),
        ])
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=create)))
    client.with_options = Mock(return_value=client)
    monkeypatch.setattr(workflow.anthropic, "AsyncAnthropic", lambda **_: client)
    monkeypatch.setattr(workflow, "load_allowed_sources", AsyncMock(return_value={"a", "b", "c", "d", "e"}))
    token = budget_scope.set("compare")
    try:
        assert await workflow.compare_search_parallelism(5) == 1
        result = await workflow.search_sources_node({"topic": "Example event", "sources": ["a", "b", "c", "d", "e"], "errors": []})
    finally:
        budget_scope.reset(token)
    assert len(result["search_results"]) == 5
    assert not result["errors"]
    assert peak == 1
    snapshot = await cost_guard.budget_snapshot()
    assert snapshot["spent_usd"] == pytest.approx(14.3025)
    assert snapshot["held_usd"] == 0
    assert snapshot["daily_limit_usd"] == 15
