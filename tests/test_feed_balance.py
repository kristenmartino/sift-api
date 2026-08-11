"""Tests for the feed-balance drift rule (ranking v2 stage 3).

Pure-function coverage of services.feed_balance.evaluate_drift — the DB and
clock stay out, mirroring tests/test_feed_health.py.
"""
from __future__ import annotations

from services.feed_balance import (
    GRIM_SHARE_DELTA,
    MIN_BASELINE,
    evaluate_drift,
)


def _history(category: str, n: int, grim: float = 0.5, civic: float = 1.0) -> list[dict]:
    return [
        {"category": category, "grim_share_top10": grim, "mean_civic_top10": civic}
        for _ in range(n)
    ]


def _snap(grim: float | None = 0.5, civic: float | None = 1.0) -> dict:
    return {"grim_share_top10": grim, "mean_civic_top10": civic}


class TestEvaluateDrift:
    def test_no_verdict_before_min_baseline(self):
        # The first days after deploy are baseline-building, not alarm-worthy —
        # even a wild value must not trip against 4 snapshots.
        trips = evaluate_drift(
            {"top": _snap(grim=1.0)}, _history("top", MIN_BASELINE - 1, grim=0.0)
        )
        assert trips == []

    def test_stable_values_do_not_trip(self):
        trips = evaluate_drift(
            {"top": _snap(grim=0.55)}, _history("top", MIN_BASELINE, grim=0.5)
        )
        assert trips == []

    def test_grim_spike_trips(self):
        trips = evaluate_drift(
            {"top": _snap(grim=0.9)}, _history("top", MIN_BASELINE, grim=0.4)
        )
        assert len(trips) == 1
        t = trips[0]
        assert (t.category, t.metric) == ("top", "grim_share_top10")
        assert t.delta == 0.5
        assert t.threshold == GRIM_SHARE_DELTA

    def test_trips_in_both_directions(self):
        # A collapse is as much a drift as a spike: grim share falling to
        # zero when the baseline says 0.5 means the dampener (or the tone
        # tagger) changed behavior, and that too should be a logged event.
        trips = evaluate_drift(
            {"top": _snap(grim=0.0)}, _history("top", MIN_BASELINE, grim=0.5)
        )
        assert [t.metric for t in trips] == ["grim_share_top10"]

    def test_civic_drift_trips_independently(self):
        trips = evaluate_drift(
            {"politics": _snap(grim=0.5, civic=2.5)},
            _history("politics", MIN_BASELINE, grim=0.5, civic=1.0),
        )
        assert [(t.category, t.metric) for t in trips] == [("politics", "mean_civic_top10")]

    def test_baselines_are_per_category(self):
        # politics history must not lend 'top' a baseline.
        trips = evaluate_drift(
            {"top": _snap(grim=1.0)}, _history("politics", MIN_BASELINE, grim=0.0)
        )
        assert trips == []

    def test_none_values_are_skipped(self):
        # An empty pool (n=0 → NULL aggregates) is a data condition, not drift.
        trips = evaluate_drift(
            {"top": _snap(grim=None, civic=None)},
            _history("top", MIN_BASELINE, grim=0.5),
        )
        assert trips == []
