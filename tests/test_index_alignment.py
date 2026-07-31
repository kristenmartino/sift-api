"""Tests for services/index_alignment.py — the one place that decides whether a
batch response may be trusted to line up with its input.

Four call sites depend on this: summarizer, context_generator,
primer_generator, entity_extractor. Before it existed, each validated only
that the model's returned index was in RANGE, which let a repeated, skipped,
or shifted index attach a result to the WRONG article (confirmed in production
2026-07-30). These pin the contract that replaced that check.
"""
from __future__ import annotations

import json
import logging

import pytest

from services.index_alignment import (
    AlignmentError,
    aligned_entries,
    log_misaligned_sub_batch,
    with_alignment_retry,
)

LOGGER = logging.getLogger("sift-api.test_index_alignment")


class TestAlignedEntries:
    def test_complete_response_is_keyed_by_index(self):
        parsed = [{"i": 1, "s": "one"}, {"i": 2, "s": "two"}]
        assert aligned_entries(parsed, 2) == {1: {"i": 1, "s": "one"}, 2: {"i": 2, "s": "two"}}

    def test_order_is_irrelevant_because_the_index_carries_the_mapping(self):
        parsed = [{"i": 3, "x": "c"}, {"i": 1, "x": "a"}, {"i": 2, "x": "b"}]
        assert [e["x"] for _, e in sorted(aligned_entries(parsed, 3).items())] == ["a", "b", "c"]

    def test_legacy_long_index_key_is_accepted(self):
        assert aligned_entries([{"index": 1}], 1) == {1: {"index": 1}}

    def test_duplicate_index_rejected(self):
        with pytest.raises(AlignmentError, match="duplicate index 1"):
            aligned_entries([{"i": 1}, {"i": 1}], 2)

    def test_gap_rejected_even_though_every_index_is_in_range(self):
        # The exact case a range check waves through.
        with pytest.raises(AlignmentError, match=r"missing indices \[2\]"):
            aligned_entries([{"i": 1}, {"i": 3}], 3)

    def test_short_response_rejected(self):
        with pytest.raises(AlignmentError, match=r"missing indices \[2, 3\]"):
            aligned_entries([{"i": 1}], 3)

    def test_empty_response_for_a_non_empty_batch_rejected(self):
        with pytest.raises(AlignmentError, match="got 0 entries for 2 inputs"):
            aligned_entries([], 2)

    def test_index_above_the_batch_rejected(self):
        with pytest.raises(AlignmentError, match="index 3 outside 1..2"):
            aligned_entries([{"i": 1}, {"i": 2}, {"i": 3}], 2)

    def test_zero_index_rejected_because_numbering_is_1_based(self):
        with pytest.raises(AlignmentError, match="index 0 outside 1..2"):
            aligned_entries([{"i": 0}, {"i": 1}], 2)

    def test_non_integer_index_rejected(self):
        with pytest.raises(AlignmentError, match="non-integer index"):
            aligned_entries([{"i": "1"}], 1)

    def test_boolean_index_rejected_despite_bool_being_an_int(self):
        # True == 1 in Python; without the explicit check it would map to the
        # first article.
        with pytest.raises(AlignmentError, match="non-integer index True"):
            aligned_entries([{"i": True}], 1)

    def test_missing_index_key_rejected(self):
        with pytest.raises(AlignmentError, match="non-integer index None"):
            aligned_entries([{"s": "no index at all"}], 1)

    def test_non_object_entry_rejected(self):
        with pytest.raises(AlignmentError, match="non-object entry"):
            aligned_entries(["just a string"], 1)


class TestWithAlignmentRetry:
    @pytest.mark.asyncio
    async def test_success_costs_exactly_one_call(self):
        calls = []

        async def call():
            calls.append(1)
            return {"ok": True}

        out = await with_alignment_retry(
            call, logger=LOGGER, event="e", batch_index=0, ids=["u1"],
        )
        assert out == {"ok": True}
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_retries_once_then_returns_the_good_response(self):
        attempts = []

        async def call():
            attempts.append(1)
            if len(attempts) == 1:
                raise AlignmentError("duplicate index 1")
            return {"ok": True}

        out = await with_alignment_retry(
            call, logger=LOGGER, event="e", batch_index=0, ids=["u1"], attempts=2,
        )
        assert out == {"ok": True}
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_reraises_after_the_last_attempt(self):
        async def call():
            raise AlignmentError("duplicate index 1")

        with pytest.raises(AlignmentError, match="duplicate index 1"):
            await with_alignment_retry(
                call, logger=LOGGER, event="e", batch_index=0, ids=["u1"], attempts=2,
            )

    @pytest.mark.asyncio
    async def test_every_attempt_logs_the_batch_ids(self, caplog):
        async def call():
            raise AlignmentError("duplicate index 1")

        with caplog.at_level(logging.INFO, logger=LOGGER.name), pytest.raises(AlignmentError):
            await with_alignment_retry(
                call, logger=LOGGER, event="my_event", batch_index=7,
                ids=["u1", "u2"], attempts=2,
            )

        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{") and '"my_event"' in r.message
        ]
        assert len(events) == 2
        assert events[0]["source_urls"] == ["u1", "u2"]
        assert events[0]["batch_index"] == 7
        assert events[0]["final"] is False
        assert events[1]["final"] is True

    @pytest.mark.asyncio
    async def test_non_alignment_errors_are_not_retried(self):
        # Transport retries belong to the SDK client, not here.
        attempts = []

        async def call():
            attempts.append(1)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await with_alignment_retry(
                call, logger=LOGGER, event="e", batch_index=0, ids=["u1"], attempts=3,
            )
        assert len(attempts) == 1


class TestLogMisalignedSubBatch:
    def test_names_every_affected_url(self, caplog):
        with caplog.at_level(logging.ERROR, logger=LOGGER.name):
            log_misaligned_sub_batch(
                LOGGER,
                event="batch_thing_misaligned",
                batch_id="batch-1",
                custom_id="thing-0",
                urls=["u1", "u2"],
                error=AlignmentError("duplicate index 1"),
            )

        payload = json.loads(caplog.records[-1].message)
        assert payload["event"] == "batch_thing_misaligned"
        assert payload["batch_id"] == "batch-1"
        assert payload["custom_id"] == "thing-0"
        assert payload["source_urls"] == ["u1", "u2"]
        assert payload["batch_size"] == 2
        assert "duplicate index 1" in payload["reason"]
