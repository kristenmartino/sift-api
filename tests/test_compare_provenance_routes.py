import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.cost_guard import BudgetDecision


@pytest.fixture
def client():
    with patch("app.db.init_pool", new_callable=AsyncMock):
        from app.main import app
        with patch("app.routers.compare.limiter.enabled", False), TestClient(app) as client:
            yield client


def result_state():
    return {
        "topic": "Example", "sources": ["Reuters", "AP", "Fox"],
        "search_results": {"AP": "Coverage", "Fox": "Coverage"},
        "analyzed_sources": ["AP", "Fox"],
        "comparison": "Two sources compared.",
        "claims": [{"claim": "Shared", "agreement": "unanimous", "sources": ["AP", "Fox"]}],
        "errors": [],
    }


def test_json_records_partial_retrieval_without_counting_the_missing_source(client):
    with patch("app.routers.compare.check_budget", new=AsyncMock(return_value=BudgetDecision(True, "ok", 0, 10))), patch(
        "app.routers.compare.compare_graph.ainvoke", new=AsyncMock(return_value=result_state()),
    ):
        response = client.post("/analyze/compare", json={"topic": "Example", "sources": ["Reuters", "AP", "Fox"]},
                               headers={"X-Pipeline-Key": "dev-key"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["sources_requested"] == ["Reuters", "AP", "Fox"]
    assert payload["sources_retrieved"] == payload["sources_analyzed"] == payload["sources_checked"] == ["AP", "Fox"]
    assert payload["sources_unavailable"] == ["Reuters"]
    assert payload["source_provenance_version"] == 1


def test_no_coverage_does_not_report_requested_sources_as_successful(client):
    state = result_state()
    state.update(search_results={}, analyzed_sources=[], claims=[], comparison="No relevant coverage.")
    with patch("app.routers.compare.check_budget", new=AsyncMock(return_value=BudgetDecision(True, "ok", 0, 10))), patch(
        "app.routers.compare.compare_graph.ainvoke", new=AsyncMock(return_value=state),
    ):
        response = client.post("/analyze/compare", json={"topic": "Example"}, headers={"X-Pipeline-Key": "dev-key"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "NO_COVERAGE"


def test_stream_has_the_same_provenance_and_filters_unsupported_sources(client):
    async def search(_client, _topic, source):
        return source, None if source == "Reuters" else "Coverage"

    extracted = {"comparison": "All three sources confirm the event.", "analyzed_sources": ["AP", "Fox"],
                 "claims": [{"claim": "Shared", "agreement": "unanimous", "sources": ["AP", "Fox", "Reuters"]}]}
    with patch("app.routers.compare.check_budget", new=AsyncMock(return_value=BudgetDecision(True, "ok", 0, 10))), patch(
        "app.routers.compare.load_allowed_sources", new=AsyncMock(return_value={"reuters", "ap", "fox"}),
    ), patch("app.routers.compare.search_one_source", side_effect=search), patch(
        "app.routers.compare.extract_and_compare_node", new=AsyncMock(return_value=extracted),
    ), patch("app.routers.compare.anthropic.AsyncAnthropic", return_value=MagicMock()):
        response = client.post("/analyze/compare/stream", json={"topic": "Example", "sources": ["Reuters", "AP", "Fox"]},
                               headers={"X-Pipeline-Key": "dev-key"})
    frames = [frame for frame in response.text.split("\n\n") if frame.startswith("event: results")]
    assert len(frames) == 1
    payload = json.loads(frames[0].split("data: ", 1)[1])
    assert payload["sources_requested"] == ["Reuters", "AP", "Fox"]
    assert payload["sources_analyzed"] == payload["sources_checked"] == ["AP", "Fox"]
    assert payload["sources_unavailable"] == ["Reuters"]
    assert payload["claims"][0]["sources"] == ["AP", "Fox"]
    assert "All three" not in payload["comparison"]
