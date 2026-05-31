from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import cost_guard


def _mock_pool(spent: float = 0.0):
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=spent)
    pool.execute = AsyncMock()
    return pool


class TestCheckBudget:
    def test_disabled_guard_always_allows_without_touching_db(self):
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", False):
            with patch.object(cost_guard, "get_pool", new_callable=AsyncMock) as gp:
                decision = asyncio.run(cost_guard.check_budget(5.0))
        assert decision.allowed is True
        assert decision.reason == "guard_disabled"
        gp.assert_not_called()

    def test_below_limit_allows(self):
        pool = _mock_pool(spent=2.0)
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard.settings, "daily_ai_cost_limit_usd", 10.0):
                with patch.object(cost_guard, "get_pool", AsyncMock(return_value=pool)):
                    decision = asyncio.run(cost_guard.check_budget(1.0))
        assert decision.allowed is True
        assert decision.reason == "within_budget"
        assert decision.spent_usd == 2.0

    def test_at_or_above_limit_blocks(self):
        pool = _mock_pool(spent=9.95)
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard.settings, "daily_ai_cost_limit_usd", 10.0):
                with patch.object(
                    cost_guard.settings, "ai_cost_alert_threshold_ratio", 0.8
                ):
                    with patch.object(
                        cost_guard, "get_pool", AsyncMock(return_value=pool)
                    ):
                        decision = asyncio.run(cost_guard.check_budget(0.10))
        assert decision.allowed is False
        assert decision.reason == "budget_exceeded"

    def test_ledger_unavailable_fails_open(self):
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(
                cost_guard, "get_pool", AsyncMock(side_effect=RuntimeError("no pool"))
            ):
                decision = asyncio.run(cost_guard.check_budget(1.0))
        assert decision.allowed is True
        assert decision.reason == "ledger_unavailable"


class TestAlert:
    def setup_method(self):
        cost_guard._alerted_dates.clear()

    def test_alert_fires_once_and_uses_sentry_when_configured(self):
        pool = _mock_pool(spent=8.5)  # 85% of 10 → over the 0.8 threshold
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard.settings, "daily_ai_cost_limit_usd", 10.0):
                with patch.object(
                    cost_guard.settings, "ai_cost_alert_threshold_ratio", 0.8
                ):
                    with patch.object(
                        cost_guard.settings,
                        "sentry_dsn",
                        "https://k@o0.ingest.sentry.io/1",
                    ):
                        with patch.object(
                            cost_guard, "get_pool", AsyncMock(return_value=pool)
                        ):
                            with patch.object(
                                cost_guard.sentry_sdk, "capture_message"
                            ) as cap:
                                d1 = asyncio.run(cost_guard.check_budget())
                                d2 = asyncio.run(cost_guard.check_budget())
        assert d1.allowed is True and d2.allowed is True
        cap.assert_called_once()  # de-duped to one alert per UTC day

    def test_alert_without_sentry_dsn_does_not_fail(self):
        pool = _mock_pool(spent=9.0)
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard.settings, "daily_ai_cost_limit_usd", 10.0):
                with patch.object(
                    cost_guard.settings, "ai_cost_alert_threshold_ratio", 0.8
                ):
                    with patch.object(cost_guard.settings, "sentry_dsn", ""):
                        with patch.object(
                            cost_guard, "get_pool", AsyncMock(return_value=pool)
                        ):
                            with patch.object(
                                cost_guard.sentry_sdk, "capture_message"
                            ) as cap:
                                decision = asyncio.run(cost_guard.check_budget())
        assert decision.allowed is True
        cap.assert_not_called()  # Sentry inert → logs only, no crash


class TestRecordUsage:
    def test_disabled_is_noop(self):
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", False):
            with patch.object(cost_guard, "get_pool", new_callable=AsyncMock) as gp:
                asyncio.run(cost_guard.record_usage("anthropic", "m", "op", 0.5))
        gp.assert_not_called()

    def test_enabled_writes_to_ledger(self):
        pool = _mock_pool()
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard, "get_pool", AsyncMock(return_value=pool)):
                asyncio.run(
                    cost_guard.record_usage(
                        "voyage", "voyage-3-lite", "embedder.embed_texts", 0.25
                    )
                )
        pool.execute.assert_called_once()

    def test_write_error_is_swallowed(self):
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(
                cost_guard, "get_pool", AsyncMock(side_effect=RuntimeError("db down"))
            ):
                # Must not raise.
                asyncio.run(cost_guard.record_usage("anthropic", "m", "op", 0.5))
