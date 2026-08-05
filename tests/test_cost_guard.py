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

    def test_proactive_estimate_blocks_before_spend_alone_would(self):
        # Spent is under the limit (and under the alert ratio), but spent plus
        # this call's estimate is not — the call is blocked before it crosses.
        pool = _mock_pool(spent=7.0)
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard.settings, "daily_ai_cost_limit_usd", 10.0):
                with patch.object(
                    cost_guard.settings, "ai_cost_alert_threshold_ratio", 0.8
                ):
                    with patch.object(
                        cost_guard, "get_pool", AsyncMock(return_value=pool)
                    ):
                        blocked = asyncio.run(cost_guard.check_budget(4.0))
                        allowed = asyncio.run(cost_guard.check_budget(0.5))
        assert blocked.allowed is False
        assert blocked.reason == "budget_exceeded"
        assert allowed.allowed is True
        assert allowed.reason == "within_budget"

    def test_ledger_unavailable_fails_closed_when_enabled(self):
        # A cost ceiling must not authorize paid calls when it can't verify
        # spend — an enabled guard fails CLOSED on a ledger/DB error.
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(
                cost_guard, "get_pool", AsyncMock(side_effect=RuntimeError("no pool"))
            ):
                decision = asyncio.run(cost_guard.check_budget(1.0))
        assert decision.allowed is False
        assert decision.reason == "guard_unavailable"


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
    def test_records_even_when_the_guard_is_disabled(self):
        """The regression guard for the 20x-stale cost figure.

        This asserted the opposite until 2026-08-05: recording used to be gated
        on `ai_cost_guard_enabled`, so the flag that turns on *blocking* also
        turned on *measuring*. With the default `false`, `ai_usage_daily` was
        never written, nobody could see it was empty, and STATUS.md quoted
        ~$15/mo against a real ~$300/mo.

        Measurement must not depend on enforcement — you need the ledger
        populated before you can choose a ceiling. Enforcement stays gated;
        that is `check_budget`'s job and is covered separately.
        """
        pool = _mock_pool()
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", False):
            with patch.object(cost_guard, "get_pool", AsyncMock(return_value=pool)):
                asyncio.run(cost_guard.record_usage("anthropic", "m", "op", 0.5))
        pool.execute.assert_called_once()

    def test_nothing_to_record_is_a_noop(self):
        pool = _mock_pool()
        with patch.object(cost_guard, "get_pool", AsyncMock(return_value=pool)):
            asyncio.run(cost_guard.record_usage("anthropic", "m", "op", 0.0, call_count=0))
        pool.execute.assert_not_called()

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
        fake_pool = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(cost_guard.settings, "ai_cost_guard_enabled", True):
            with patch.object(cost_guard, "get_pool", fake_pool):
                # Must not raise — telemetry never breaks the pipeline.
                asyncio.run(cost_guard.record_usage("anthropic", "m", "op", 0.5))

        # Assert it actually reached the DB and swallowed a real failure. Without
        # this, the test would still pass if record_usage short-circuited before
        # the write and never attempted anything at all.
        fake_pool.assert_called_once()
