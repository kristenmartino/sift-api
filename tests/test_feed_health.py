"""Tests for services/feed_health.py (#125).

The rule exists to separate two things a row count cannot: an outlet that has
DIED, and one that is simply low-volume. Prod's healthy outlets span 21 to
4,203 rows per 14 days, so every case below is built from a real outlet's
measured shape rather than an invented one.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from services.feed_health import (
    MAX_SILENCE_DAYS,
    STALE_FLOOR_DAYS,
    evaluate_feed_health,
    leash_for,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _row(source: str, days_ago: float, active_days: int) -> dict:
    return {
        "source_name": source,
        "last_new_row": NOW - timedelta(days=days_ago),
        "active_days": active_days,
    }


def _verdicts(configured, rows, now=NOW) -> dict[str, str]:
    return {r.source_name: r.verdict for r in evaluate_feed_health(configured, rows, now)}


class TestTheRule:
    def test_a_busy_outlet_publishing_today_is_ok(self):
        # NPR: thousands of rows, active every day.
        assert _verdicts(["NPR"], [_row("NPR", 0.1, 14)]) == {"NPR": "ok"}

    def test_an_outlet_that_had_a_habit_and_broke_it_is_stalled(self):
        # The Washington Post case: active daily, then HTTP 400 for 15 days.
        assert _verdicts(["WaPo"], [_row("WaPo", 15.4, 14)]) == {"WaPo": "stalled"}

    def test_a_low_volume_outlet_going_quiet_is_not_a_fault(self):
        # WHO: 15 rows across 5 of 14 days. Silence is its normal state, and
        # paging on it would train everyone to ignore this check.
        assert _verdicts(["WHO"], [_row("WHO", 4.0, 5)]) == {"WHO": "quiet"}

    def test_a_sparse_but_reliable_outlet_is_still_held_to_the_rule(self):
        # ProPublica: only 17 rows, but spread over 12 of 14 days. That is a
        # habit, so a 5-day gap is a real signal.
        assert _verdicts(["ProPublica"], [_row("ProPublica", 5.0, 12)]) == {"ProPublica": "stalled"}

    def test_an_outlet_that_never_produced_anything_is_flagged_immediately(self):
        # USA Today sat in FEEDS for its entire life without ever ingesting.
        # There is no habit to break and no window to wait for.
        assert _verdicts(["USA Today"], []) == {"USA Today": "never"}

    def test_outlets_no_longer_configured_are_ignored(self):
        # Prod holds 125 distinct source_names against 56 configured outlets —
        # outlets pruned on purpose are not faults.
        results = evaluate_feed_health(["NPR"], [_row("NPR", 0.1, 14), _row("Retired", 400.0, 0)], NOW)
        assert [r.source_name for r in results] == ["NPR"]


class TestTheLeash:
    """Each outlet is judged against its OWN rhythm, not a shared threshold."""

    def test_a_daily_outlet_gets_only_the_floor(self):
        assert leash_for(14) == STALE_FLOOR_DAYS

    def test_a_sparse_outlet_earns_a_proportionally_longer_one(self):
        # WHO: 5 active days of 14 -> a typical gap of 2.8d -> 8.4d of leash.
        assert leash_for(5) == pytest.approx(8.4, abs=0.05)
        assert leash_for(5) > leash_for(12) > STALE_FLOOR_DAYS - 0.01

    def test_nobody_outruns_the_window(self):
        # Producing nothing for 14 days is dead by any reading.
        assert leash_for(1) == MAX_SILENCE_DAYS
        assert leash_for(0) == MAX_SILENCE_DAYS

    def test_just_inside_the_leash_is_quiet_not_stalled(self):
        assert _verdicts(["X"], [_row("X", 8.3, 5)]) == {"X": "quiet"}

    def test_just_outside_the_leash_stalls(self):
        assert _verdicts(["X"], [_row("X", 8.5, 5)]) == {"X": "stalled"}

    def test_a_brisk_outlet_silent_past_the_floor_stalls_immediately(self):
        assert _verdicts(["X"], [_row("X", STALE_FLOOR_DAYS, 14)]) == {"X": "stalled"}

    def test_under_the_floor_is_ok_however_sparse(self):
        assert _verdicts(["X"], [_row("X", STALE_FLOOR_DAYS - 0.01, 1)]) == {"X": "ok"}

    def test_a_null_active_days_does_not_crash_the_check(self):
        # COUNT(...) FILTER returns 0, but a NULL here must not take the
        # monitor down — it would silence the very thing it exists to report.
        row = _row("X", 10.0, 0)
        row["active_days"] = None
        assert _verdicts(["X"], [row]) == {"X": "stalled"}


class TestCheckFeedHealth:
    @staticmethod
    def _pool(rows):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=rows)
        pool.fetchval = AsyncMock(return_value=NOW)
        return pool

    @pytest.mark.asyncio
    async def test_emits_one_structured_event_naming_the_broken_outlets(self, monkeypatch, caplog):
        from services import feed_health

        monkeypatch.setattr("services.rss.FEEDS", [("NPR", "u1"), ("WaPo", "u2"), ("Ghost", "u3")])
        pool = self._pool([_row("NPR", 0.1, 14), _row("WaPo", 15.4, 14)])

        with caplog.at_level(logging.INFO, logger="sift-api.feed_health"):
            payload = await feed_health.check_feed_health(pool)

        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{") and '"feed_health"' in r.message
        ]
        assert len(events) == 1
        assert events[0] == payload
        assert payload["outlets_total"] == 3
        assert payload["outlets_ok"] == 1
        assert [s["source"] for s in payload["stalled"]] == ["WaPo"]
        assert [s["source"] for s in payload["never_ingested"]] == ["Ghost"]
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_healthy_run_says_nothing_louder_than_info(self, monkeypatch, caplog):
        from services import feed_health

        monkeypatch.setattr("services.rss.FEEDS", [("NPR", "u1")])
        pool = self._pool([_row("NPR", 0.1, 14)])

        with caplog.at_level(logging.INFO, logger="sift-api.feed_health"):
            payload = await feed_health.check_feed_health(pool)

        assert payload["stalled"] == [] and payload["never_ingested"] == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_quiet_outlets_are_reported_but_do_not_raise_the_alarm(self, monkeypatch, caplog):
        """The whole point of the two-part rule: visible, not alarming."""
        from services import feed_health

        monkeypatch.setattr("services.rss.FEEDS", [("WHO", "u1")])
        pool = self._pool([_row("WHO", 6.0, 5)])

        with caplog.at_level(logging.INFO, logger="sift-api.feed_health"):
            payload = await feed_health.check_feed_health(pool)

        assert [q["source"] for q in payload["quiet"]] == ["WHO"]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestMonotonicity:
    """Getting worse must never look like getting better.

    Caught by replaying Washington Post day by day against prod: it stalled on
    07-20, then flickered back to "quiet" on 07-28. The leash is measured from
    RECENT activity, so a feed dead long enough has none left in the window and
    its leash widens to the cap — the longer it stayed broken, the more slack
    it earned.
    """

    def test_an_outlet_with_no_activity_in_the_whole_window_is_stalled(self):
        assert _verdicts(["X"], [_row("X", 12.3, 0)]) == {"X": "stalled"}

    def test_the_verdict_never_softens_as_the_silence_grows(self):
        """`active_days` is anchored to the outlet's last row, so it is FIXED
        while the outage runs. Only days_since moves, and only upward."""
        active_when_alive = 14  # WaPo published daily right up to the failure
        verdicts = [
            _verdicts(["X"], [_row("X", d, active_when_alive)])["X"]
            for d in (0.5, 2.9, 3.0, 4.3, 7.3, 11.3, 12.3, 15.4, 40.0)
        ]
        assert verdicts == ["ok", "ok", "stalled", "stalled", "stalled",
                            "stalled", "stalled", "stalled", "stalled"], verdicts

    def test_a_sparse_outlet_hardens_in_the_same_one_way_direction(self):
        active_when_alive = 5  # WHO: leash 8.4d
        verdicts = [
            _verdicts(["X"], [_row("X", d, active_when_alive)])["X"]
            for d in (1.0, 3.0, 6.0, 8.3, 8.5, 20.0)
        ]
        assert verdicts == ["ok", "quiet", "quiet", "quiet", "stalled", "stalled"], verdicts
