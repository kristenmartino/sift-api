import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cost_guard import BudgetDecision
from services.daily_compare import _refresh


@pytest.mark.parametrize("analyzed", [["Reuters", "Associated Press"], []])
def test_daily_example_only_persists_completed_multi_source_analysis(analyzed):
    conn = AsyncMock()
    conn.fetchrow.return_value = {"title": "Example story"}
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    state = {
        "sources": ["Reuters", "Associated Press", "Fox News"],
        "search_results": {"Reuters": "Coverage", "Associated Press": "Coverage"},
        "analyzed_sources": analyzed, "comparison": "Two sources compared.", "claims": [],
    }
    with patch("services.daily_compare.get_pool", new=AsyncMock(return_value=pool)), patch(
        "services.daily_compare.check_budget", new=AsyncMock(return_value=BudgetDecision(True, "ok", 0, 10)),
    ), patch("services.daily_compare._graph.ainvoke", new=AsyncMock(return_value=state)):
        refreshed = asyncio.run(_refresh(force=True))
    if not analyzed:
        assert not refreshed
        conn.execute.assert_not_awaited()
    else:
        assert refreshed
        payload = json.loads(conn.execute.call_args.args[1])
        assert payload["sources_analyzed"] == payload["sources_checked"] == analyzed
        assert payload["sources_unavailable"] == ["Fox News"]
        assert payload["source_provenance_version"] == 1
