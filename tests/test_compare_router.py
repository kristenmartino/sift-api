from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.cost_guard import BudgetDecision


@pytest.fixture
def client():
    """Create a test client with mocked DB."""
    with patch("app.db._pool", None):
        with patch("app.db.init_pool", new_callable=AsyncMock):
            from app.main import app
            with TestClient(app) as c:
                yield c


class TestCompareAuth:
    def test_missing_key_returns_422(self, client):
        """Missing X-Pipeline-Key header returns 422."""
        response = client.post(
            "/analyze/compare",
            json={"topic": "climate change"},
        )
        assert response.status_code == 422

    def test_wrong_key_returns_401(self, client):
        """Wrong API key returns 401."""
        response = client.post(
            "/analyze/compare",
            json={"topic": "climate change"},
            headers={"X-Pipeline-Key": "wrong-key"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid pipeline key"


class TestCompareInputValidation:
    def test_topic_too_short(self, client):
        """Topic shorter than 3 characters is rejected."""
        response = client.post(
            "/analyze/compare",
            json={"topic": "ab"},
            headers={"X-Pipeline-Key": "dev-key"},
        )
        assert response.status_code == 422

    def test_topic_too_long(self, client):
        """Topic longer than 500 characters is rejected."""
        response = client.post(
            "/analyze/compare",
            json={"topic": "x" * 501},
            headers={"X-Pipeline-Key": "dev-key"},
        )
        assert response.status_code == 422

    def test_too_many_sources(self, client):
        """More than 5 sources is rejected."""
        response = client.post(
            "/analyze/compare",
            json={
                "topic": "climate change",
                "sources": ["a", "b", "c", "d", "e", "f"],
            },
            headers={"X-Pipeline-Key": "dev-key"},
        )
        assert response.status_code == 422

    def test_valid_request_accepted(self, client):
        """Valid request with correct auth passes validation."""
        mock_result = {
            "topic": "climate change",
            "sources": ["reuters", "bbc"],
            "search_results": {},
            "claims": [{"claim": "test", "agreement": "unanimous", "sources": ["reuters"]}],
            "comparison": "Sources agree.",
            "errors": [],
        }

        with patch(
            "app.routers.compare.compare_graph.ainvoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.post(
                "/analyze/compare",
                json={"topic": "climate change", "sources": ["reuters", "bbc"]},
                headers={"X-Pipeline-Key": "dev-key"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["topic"] == "climate change"
            assert "comparison" in data


class TestCompareErrorSanitization:
    def test_500_does_not_leak_details(self, client):
        """Internal errors don't expose exception details."""
        with patch(
            "app.routers.compare.compare_graph.ainvoke",
            new_callable=AsyncMock,
            side_effect=RuntimeError("secret database connection string here"),
        ):
            response = client.post(
                "/analyze/compare",
                json={"topic": "climate change"},
                headers={"X-Pipeline-Key": "dev-key"},
            )
            assert response.status_code == 500
            detail = response.json()["detail"]
            assert "secret" not in str(detail)
            assert detail["detail"] == "Comparison failed"
            assert detail["code"] == "COMPARISON_FAILED"


class TestCompareCostCeiling:
    def test_over_budget_blocks_without_calling_provider(self, client):
        """When today's spend is over budget, the request is rejected 503 and the
        compare workflow (the paid web-search path) is never invoked."""
        blocked = BudgetDecision(False, "budget_exceeded", 11.0, 10.0)
        with patch(
            "app.routers.compare.check_budget",
            new_callable=AsyncMock,
            return_value=blocked,
        ):
            with patch(
                "app.routers.compare.compare_graph.ainvoke", new_callable=AsyncMock
            ) as ainvoke:
                response = client.post(
                    "/analyze/compare",
                    json={"topic": "climate change", "sources": ["reuters"]},
                    headers={"X-Pipeline-Key": "dev-key"},
                )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "AI_BUDGET_EXCEEDED"
        ainvoke.assert_not_called()

    def test_guard_unavailable_fails_closed_without_calling_provider(self, client):
        """Fail-closed: when the guard can't verify budget (e.g. DB error), the
        request is rejected 503 and the provider is never called."""
        blocked = BudgetDecision(False, "guard_unavailable", 0.0, 10.0)
        with patch(
            "app.routers.compare.check_budget",
            new_callable=AsyncMock,
            return_value=blocked,
        ):
            with patch(
                "app.routers.compare.compare_graph.ainvoke", new_callable=AsyncMock
            ) as ainvoke:
                response = client.post(
                    "/analyze/compare",
                    json={"topic": "climate change", "sources": ["reuters"]},
                    headers={"X-Pipeline-Key": "dev-key"},
                )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "COST_GUARD_UNAVAILABLE"
        ainvoke.assert_not_called()
