"""The batch poller must issue zero queries while nothing is in flight.

This is the property that took the Neon compute from "never suspends" to
"suspends between pipeline runs". The old poller ran a SELECT against
api_batches every 60 seconds forever; each of those landed inside Neon's 300s
scale-to-zero window and reset it, so the compute billed ~730 CU-hours a month
against a 300 CU-hour allowance (measured 2026-08-14: 26 days of unbroken
pg_postmaster_start_time uptime).

Without these tests the regression is invisible — re-adding a status SELECT
would look like a harmless simplification and break nothing that anyone runs.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import batch_client
from services.batch_client import poll_pending_batches


class TestIdlePollerTouchesNothing:
    @pytest.mark.asyncio
    async def test_poll_with_nothing_pending_makes_no_db_call(self):
        """The load-bearing assertion: idle poll, zero queries."""
        pool = AsyncMock()

        with patch("services.batch_client.get_pool", new_callable=AsyncMock, return_value=pool) as gp:
            await poll_pending_batches({})

        assert gp.await_count == 0
        pool.fetch.assert_not_awaited()
        pool.execute.assert_not_awaited()
        pool.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poll_with_nothing_pending_makes_no_anthropic_call(self):
        """It should not reach for the Anthropic client either."""
        with patch("services.batch_client._client") as client_factory:
            await poll_pending_batches({})

        client_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_batch_is_polled_but_db_untouched_until_it_ends(self):
        """While a batch is merely in flight, Anthropic is asked and Postgres is not."""
        batch_client.register_pending("batch_abc", "context")

        still_running = MagicMock()
        still_running.processing_status = "in_progress"
        client = MagicMock()
        client.messages.batches.retrieve = AsyncMock(return_value=still_running)

        pool = AsyncMock()
        with patch("services.batch_client._client", return_value=client):
            with patch("services.batch_client.get_pool", new_callable=AsyncMock, return_value=pool) as gp:
                await poll_pending_batches({})

        client.messages.batches.retrieve.assert_awaited_once_with("batch_abc")
        assert gp.await_count == 0
        assert batch_client.has_pending() is True


class TestSignalLifecycle:
    @pytest.mark.asyncio
    async def test_wait_returns_immediately_once_a_batch_is_registered(self):
        batch_client.register_pending("batch_abc", "context")

        await asyncio.wait_for(batch_client.wait_for_pending(), timeout=1.0)

        assert batch_client.has_pending() is True
        assert batch_client._pending == {"batch_abc": "context"}

    @pytest.mark.asyncio
    async def test_wait_blocks_while_nothing_is_pending(self):
        """Idle means blocked, not spinning on a timer."""
        assert batch_client.has_pending() is False
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(batch_client.wait_for_pending(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_forgetting_the_last_batch_blocks_again(self):
        batch_client.register_pending("batch_abc", "context")
        batch_client._forget("batch_abc")

        assert batch_client.has_pending() is False
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(batch_client.wait_for_pending(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_forgetting_one_of_two_keeps_the_poller_awake(self):
        batch_client.register_pending("batch_abc", "context")
        batch_client.register_pending("batch_def", "primer")
        batch_client._forget("batch_abc")

        assert batch_client.has_pending() is True
        await asyncio.wait_for(batch_client.wait_for_pending(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_register_is_idempotent(self):
        batch_client.register_pending("batch_abc", "context")
        first_seen = batch_client._submitted_at["batch_abc"]
        batch_client.register_pending("batch_abc", "context")

        # The give-up clock must not be reset by a repeat registration —
        # otherwise a batch re-adopted by every reconcile could never expire.
        assert batch_client._submitted_at["batch_abc"] == first_seen
        assert len(batch_client._pending) == 1


class TestGiveUpBound:
    @pytest.mark.asyncio
    async def test_a_batch_past_the_giveup_window_is_expired_and_dropped(self):
        """A batch Anthropic loses must not keep the compute awake forever.

        The old code retried indefinitely: a batch whose results download kept
        failing was polled every 60s until the process died.
        """
        from datetime import datetime, timedelta, timezone

        batch_client.register_pending("batch_old", "context")
        batch_client._submitted_at["batch_old"] = datetime.now(timezone.utc) - timedelta(
            hours=batch_client.BATCH_GIVEUP_HOURS + 1
        )

        pool = AsyncMock()
        client = MagicMock()
        client.messages.batches.retrieve = AsyncMock()

        with patch("services.batch_client._client", return_value=client):
            with patch("services.batch_client.get_pool", new_callable=AsyncMock, return_value=pool):
                await poll_pending_batches({})

        # Expired without asking Anthropic again, and removed from the set.
        client.messages.batches.retrieve.assert_not_awaited()
        assert batch_client.has_pending() is False
        args = pool.execute.await_args[0]
        assert "expired" in args


class TestRecovery:
    @pytest.mark.asyncio
    async def test_sync_adopts_rows_left_in_flight_by_a_previous_process(self):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[
            {"batch_id": "batch_abc", "kind": "context"},
            {"batch_id": "batch_def", "kind": "primer"},
        ])

        with patch("services.batch_client.get_pool", new_callable=AsyncMock, return_value=pool):
            adopted = await batch_client.sync_pending_from_db()

        assert adopted == 2
        assert batch_client.has_pending() is True
        await asyncio.wait_for(batch_client.wait_for_pending(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_sync_does_not_double_count_already_known_batches(self):
        batch_client.register_pending("batch_abc", "context")

        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=[{"batch_id": "batch_abc", "kind": "context"}])

        with patch("services.batch_client.get_pool", new_callable=AsyncMock, return_value=pool):
            adopted = await batch_client.sync_pending_from_db()

        assert adopted == 0
        assert len(batch_client._pending) == 1
