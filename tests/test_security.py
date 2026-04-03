from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client with mocked DB."""
    with patch("app.db._pool", None):
        with patch("app.db.init_pool", new_callable=AsyncMock):
            from app.main import app
            with TestClient(app) as c:
                yield c


class TestSecurityHeaders:
    def test_x_content_type_options(self, client):
        response = client.get("/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_x_frame_options(self, client):
        response = client.get("/health")
        assert response.headers["X-Frame-Options"] == "DENY"

    def test_content_security_policy(self, client):
        response = client.get("/health")
        assert response.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_permissions_policy(self, client):
        response = client.get("/health")
        assert response.headers["Permissions-Policy"] == "()"

    def test_content_language(self, client):
        response = client.get("/health")
        assert response.headers["Content-Language"] == "en"

    def test_request_id_generated(self, client):
        response = client.get("/health")
        assert "X-Request-ID" in response.headers
        # Should be a valid UUID
        import uuid
        uuid.UUID(response.headers["X-Request-ID"])

    def test_request_id_echoed(self, client):
        custom_id = "test-request-123"
        response = client.get("/health", headers={"X-Request-ID": custom_id})
        assert response.headers["X-Request-ID"] == custom_id

    def test_no_deprecated_xss_protection(self, client):
        response = client.get("/health")
        assert "X-XSS-Protection" not in response.headers

    def test_referrer_policy(self, client):
        response = client.get("/health")
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_hsts_not_in_development(self, client):
        """HSTS header should not be present in development."""
        response = client.get("/health")
        assert "Strict-Transport-Security" not in response.headers

    def test_hsts_in_production(self):
        """HSTS header should be present in production."""
        with patch("app.config.settings.environment", "production"):
            with patch("app.db._pool", None):
                with patch("app.db.init_pool", new_callable=AsyncMock):
                    from app.main import app
                    with TestClient(app) as c:
                        response = c.get("/health")
                        assert "Strict-Transport-Security" in response.headers
                        assert "max-age=63072000" in response.headers["Strict-Transport-Security"]


class TestCORSHeaders:
    def test_cors_allows_listed_origin(self, client):
        """Requests from allowed origins get CORS headers."""
        response = client.options(
            "/health",
            headers={
                "Origin": "https://siftnews.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.headers.get("access-control-allow-origin") == "https://siftnews.ai"

    def test_cors_blocks_unlisted_origin(self, client):
        """Requests from unlisted origins don't get CORS allow header."""
        response = client.options(
            "/health",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_cors_allows_pipeline_key_header(self, client):
        """X-Pipeline-Key is in the allowed headers list."""
        response = client.options(
            "/pipeline/refresh",
            headers={
                "Origin": "https://siftnews.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Pipeline-Key",
            },
        )
        allowed = response.headers.get("access-control-allow-headers", "")
        assert "x-pipeline-key" in allowed.lower()


class TestPipelineErrorSanitization:
    def test_500_does_not_leak_details(self):
        """Pipeline 500 errors don't expose exception details."""
        with patch("app.db._pool", None):
            with patch("app.db.init_pool", new_callable=AsyncMock):
                from app.main import app
                with TestClient(app) as client:
                    with patch(
                        "app.routers.pipeline.pipeline.ainvoke",
                        new_callable=AsyncMock,
                        side_effect=RuntimeError("internal DB password=secret123"),
                    ):
                        response = client.post(
                            "/pipeline/refresh",
                            json={},
                            headers={"X-Pipeline-Key": "dev-key"},
                        )
                        assert response.status_code == 500
                        body = response.json()
                        detail = body["detail"]
                        assert "secret123" not in str(detail)
                        assert detail["detail"] == "Pipeline execution failed"
                        assert detail["code"] == "PIPELINE_FAILED"
