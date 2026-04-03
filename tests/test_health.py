from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client with mocked DB."""
    # Patch db pool before importing the app
    with patch("app.db._pool", None):
        with patch("app.db.init_pool", new_callable=AsyncMock):
            from app.main import app
            with TestClient(app) as c:
                yield c


class TestHealthEndpoint:
    def test_health_no_db(self, client):
        """Health endpoint returns degraded when DB is unavailable."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["version"] == "1.0.0"
        assert data["db_connected"] is False

    def test_health_with_db(self):
        """Health endpoint shows db_connected when pool works."""
        mock_pool = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=1)
        mock_pool.fetchrow = AsyncMock(return_value={"last_run": None})

        with patch("app.main.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            with patch("app.db.init_pool", new_callable=AsyncMock):
                from app.main import app
                with TestClient(app) as client:
                    response = client.get("/health")
                    assert response.status_code == 200
                    data = response.json()
                    assert data["status"] == "healthy"
                    assert data["db_connected"] is True
