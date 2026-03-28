from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client with mocked pipeline."""
    with patch("app.db._pool", None):
        with patch("app.db.init_pool", new_callable=AsyncMock):
            from app.main import app
            with TestClient(app) as c:
                yield c


class TestPipelineAuth:
    def test_missing_key_returns_422(self, client):
        """Missing X-Pipeline-Key header returns 422."""
        response = client.post(
            "/pipeline/refresh",
            json={"categories": ["technology"]},
        )
        assert response.status_code == 422

    def test_wrong_key_returns_401(self, client):
        """Wrong API key returns 401."""
        response = client.post(
            "/pipeline/refresh",
            json={"categories": ["technology"]},
            headers={"X-Pipeline-Key": "wrong-key"},
        )
        assert response.status_code == 401

    def test_invalid_category_returns_400(self, client):
        """Invalid category name returns 400."""
        response = client.post(
            "/pipeline/refresh",
            json={"categories": ["invalid_category"]},
            headers={"X-Pipeline-Key": "dev-key"},
        )
        assert response.status_code == 400
        assert "invalid_category" in response.json()["detail"]


class TestPipelineExecution:
    def test_successful_pipeline(self, client):
        """Pipeline runs and returns results."""
        mock_result = {
            "categories": ["technology"],
            "force": False,
            "articles": [],
            "new_articles": [],
            "summaries": {},
            "embeddings": {},
            "results": {
                "technology": {"new_articles": 5, "skipped": 10, "errors": 0}
            },
            "errors": [],
        }

        with patch(
            "app.routers.pipeline.pipeline.ainvoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.post(
                "/pipeline/refresh",
                json={"categories": ["technology"]},
                headers={"X-Pipeline-Key": "dev-key"},
            )
            assert response.status_code == 200
            data = response.json()
            assert "technology" in data["results"]
            assert data["results"]["technology"]["new_articles"] == 5
            assert "duration_ms" in data

    def test_default_categories(self, client):
        """Request with no categories uses all 7 defaults."""
        mock_result = {
            "results": {},
            "errors": [],
        }

        with patch(
            "app.routers.pipeline.pipeline.ainvoke",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            response = client.post(
                "/pipeline/refresh",
                json={},
                headers={"X-Pipeline-Key": "dev-key"},
            )
            assert response.status_code == 200
