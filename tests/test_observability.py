from __future__ import annotations

from unittest.mock import patch

from app import observability


class TestInitSentry:
    def test_noop_without_dsn(self):
        """Without a DSN, init_sentry returns False and never calls sentry_sdk.init."""
        with patch.object(observability.settings, "sentry_dsn", ""):
            with patch.object(observability.sentry_sdk, "init") as mock_init:
                result = observability.init_sentry()
        assert result is False
        mock_init.assert_not_called()

    def test_initializes_with_dsn(self):
        """With a DSN set, sentry_sdk.init is called once with PII disabled and
        our environment + traces rate passed through."""
        dsn = "https://examplePublicKey@o0.ingest.sentry.io/0"
        with patch.object(observability.settings, "sentry_dsn", dsn):
            with patch.object(observability.settings, "environment", "production"):
                with patch.object(
                    observability.settings, "sentry_traces_sample_rate", 0.25
                ):
                    with patch.object(observability.sentry_sdk, "init") as mock_init:
                        result = observability.init_sentry()
        assert result is True
        mock_init.assert_called_once()
        kwargs = mock_init.call_args.kwargs
        assert kwargs["send_default_pii"] is False
        assert kwargs["dsn"] == dsn
        assert kwargs["environment"] == "production"
        assert kwargs["traces_sample_rate"] == 0.25
