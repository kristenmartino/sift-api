from __future__ import annotations

from datetime import datetime, timezone

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

    def test_health_makes_no_query(self, client):
        """The default /health path must not touch Postgres.

        This is the whole point of app/pipeline_clock.py. The GitHub heartbeat
        calls /health every 30 minutes; a query here lands inside Neon's 300s
        scale-to-zero window and holds the compute open indefinitely. If
        someone reintroduces a SELECT, this fails.
        """
        from app import pipeline_clock

        pipeline_clock.seed(datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc), db_ok=True)

        mock_pool = AsyncMock()
        with patch("app.main.get_pool", new_callable=AsyncMock, return_value=mock_pool) as gp:
            response = client.get("/health")

        assert response.status_code == 200
        assert gp.await_count == 0
        mock_pool.fetchval.assert_not_awaited()
        mock_pool.fetchrow.assert_not_awaited()

        data = response.json()
        assert data["db_connected"] is True
        assert data["last_pipeline_run"] == "2026-08-14T09:00:00+00:00"

    def test_health_serves_seeded_timestamp_when_db_is_down(self, client):
        """A DB outage must not erase the last known pipeline run.

        The old handler swallowed every exception and returned
        last_pipeline_run: null, which the heartbeat reads as "stale" — so an
        outage produced a self-heal POST that could not possibly succeed.
        Reporting the last known run plus status=degraded is the truer signal.
        """
        from app import pipeline_clock

        pipeline_clock.seed(datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc), db_ok=True)

        failing_pool = AsyncMock()
        failing_pool.fetchval = AsyncMock(side_effect=RuntimeError("neon unreachable"))

        with patch("app.main.get_pool", new_callable=AsyncMock, return_value=failing_pool):
            response = client.get("/health?deep=1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["db_connected"] is False
        # Still reported, despite the probe failing.
        assert data["last_pipeline_run"] == "2026-08-14T09:00:00+00:00"

    def test_health_deep_probes_the_database(self, client):
        """?deep=1 is the opt-in live check, and does run SELECT 1."""
        from app import pipeline_clock

        pipeline_clock.seed(None, db_ok=False)

        mock_pool = AsyncMock()
        mock_pool.fetchval = AsyncMock(return_value=1)

        with patch("app.main.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            response = client.get("/health?deep=1")

        assert response.status_code == 200
        mock_pool.fetchval.assert_awaited_once()
        data = response.json()
        assert data["status"] == "healthy"
        assert data["db_connected"] is True


class TestPipelineClock:
    def test_note_pipeline_run_advances_the_clock(self):
        from app import pipeline_clock

        assert pipeline_clock.last_pipeline_run() is None
        pipeline_clock.note_pipeline_run()
        assert pipeline_clock.last_pipeline_run() is not None
        # A successful write is also evidence the database is reachable.
        assert pipeline_clock.db_ok() is True

    def test_note_db_error_does_not_erase_the_timestamp(self):
        from app import pipeline_clock

        stamp = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
        pipeline_clock.seed(stamp, db_ok=True)
        pipeline_clock.note_db_error()

        assert pipeline_clock.db_ok() is False
        assert pipeline_clock.last_pipeline_run() == stamp

    @pytest.mark.asyncio
    async def test_store_node_advances_the_clock(self):
        """store_node is the sole writer of pipeline_state, so it owns the clock.

        The update lives in store_node rather than in the scheduled loop
        specifically so that POST /pipeline/refresh — the heartbeat's self-heal
        path — also advances it. If it drifted up into _scheduled_refresh, a
        self-heal would fix ingestion while leaving /health reporting stale,
        and the heartbeat would keep firing.
        """
        from unittest.mock import AsyncMock, patch

        from app import pipeline_clock
        from workflows.pipeline_workflow import store_node

        assert pipeline_clock.last_pipeline_run() is None

        pool = AsyncMock()
        pool.fetchval = AsyncMock(return_value=0)

        state = {
            "force": False,
            "articles": [],
            "new_articles": [],
            "summaries": {},
            "embeddings": {},
            "results": {},
            "total_skipped": 0,
            "errors": [],
        }

        with patch("app.db.get_pool", new_callable=AsyncMock, return_value=pool):
            await store_node(state)

        assert pipeline_clock.last_pipeline_run() is not None
        assert pipeline_clock.db_ok() is True
