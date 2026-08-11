"""Tests for the output-stop instrumentation (migration 021).

WHY THIS FILE EXISTS
--------------------
`summarizer.batch` re-asks 4-12% of its calls for misalignment and nothing
recorded why. These tests cover the recording path, and specifically the two
ways instrumentation like this goes wrong in this repo's history:

1. **It breaks the thing it measures.** A telemetry write that raises would
   take ingest down with it — so the recorder must swallow everything.
2. **It cannot observe the failure it was added for.** #113's shadow logger
   could not write, so it could never have found a write bug. Here the
   equivalent is recording only successful calls: the whole question is what
   the *misaligned* ones did, so `test_records_the_misaligned_call` is the
   load-bearing test.
"""
import asyncio
from unittest.mock import patch

import pytest

from services.index_alignment import AlignmentError
from services.usage_tracker import log_output_stop


class _Usage:
    def __init__(self, output_tokens):
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, stop_reason="end_turn", output_tokens=300):
        self.stop_reason = stop_reason
        self.usage = _Usage(output_tokens)


class TestLogOutputStop:
    def test_no_event_loop_is_a_noop(self):
        """Sync contexts and unit tests must not blow up — mirrors _record_to_ledger.

        Asserts the write was *not* attempted, not merely that nothing raised:
        without a loop there is nothing to schedule onto, and a version that
        tried anyway would fail somewhere less obvious.
        """
        attempted = []
        with patch("services.cost_guard.record_output_stop",
                   lambda *a, **kw: attempted.append(kw)):
            log_output_stop("summarizer.batch", _Response(), aligned=True, batch_size=5)
        assert attempted == []

    @pytest.mark.asyncio
    async def test_schedules_a_write_with_the_response_fields(self):
        seen = {}

        async def fake_record(operation, stop_reason, **kw):
            seen.update({"operation": operation, "stop_reason": stop_reason, **kw})

        with patch("services.cost_guard.record_output_stop", fake_record):
            log_output_stop(
                "summarizer.batch",
                _Response(stop_reason="max_tokens", output_tokens=700),
                aligned=False,
                batch_size=5,
            )
            await asyncio.sleep(0)  # let the scheduled task run

        assert seen["operation"] == "summarizer.batch"
        assert seen["stop_reason"] == "max_tokens"
        assert seen["aligned"] is False
        assert seen["output_tokens"] == 700
        assert seen["batch_size"] == 5

    @pytest.mark.asyncio
    async def test_a_response_missing_usage_degrades_rather_than_skipping(self):
        """A response with no `usage` still records — with zero tokens.

        Degrading beats dropping: the stop_reason is the measurement, and
        losing the whole row because token counts were absent would bias the
        rate toward whatever shape happens to carry usage.
        """
        class Bare:
            stop_reason = "end_turn"

        seen = {}

        async def fake_record(operation, stop_reason, **kw):
            seen.update({"stop_reason": stop_reason, **kw})

        with patch("services.cost_guard.record_output_stop", fake_record):
            log_output_stop("summarizer.batch", Bare(), aligned=True, batch_size=5)
            await asyncio.sleep(0)

        assert seen["stop_reason"] == "end_turn"
        assert seen["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_a_failing_write_never_reaches_the_caller(self):
        """Telemetry must not break ingest. The task fails; the caller does not.

        Asserts the write was actually *attempted* as well as swallowed —
        "nothing raised" is also true of an instrument that does nothing at
        all, which is the #113 failure this suite exists to prevent.
        """
        attempted = []

        async def boom(*a, **kw):
            attempted.append(kw)
            raise RuntimeError("db down")

        with patch("services.cost_guard.record_output_stop", boom):
            log_output_stop("summarizer.batch", _Response(), aligned=True, batch_size=5)
            await asyncio.sleep(0)

        assert len(attempted) == 1, "swallowed an exception that was never raised"


class TestAlignmentErrorCarriesResponseContext:
    def test_defaults_are_none_when_nobody_fills_them(self):
        """`aligned_entries` raises without a response in hand — that must work."""
        e = AlignmentError("missing indices [3]")
        assert e.stop_reason is None
        assert e.output_tokens is None
        assert e.max_output_tokens is None

    def test_retry_log_reports_the_stop_reason(self):
        """The point of the attributes: the retry event names why it misaligned."""
        import json
        import logging

        from services.index_alignment import with_alignment_retry

        records = []

        class _Cap(logging.Logger):
            def info(self, msg, *a, **kw):
                records.append(msg)

            def warning(self, *a, **kw):
                pass

        async def always_truncated():
            e = AlignmentError("unterminated array")
            e.stop_reason = "max_tokens"
            e.output_tokens = 700
            e.max_output_tokens = 700
            raise e

        with pytest.raises(AlignmentError):
            asyncio.run(with_alignment_retry(
                always_truncated,
                logger=_Cap("t"),
                event="summary_batch_misaligned",
                batch_index=0,
                ids=["u1", "u2"],
            ))

        payloads = [json.loads(r) for r in records]
        assert payloads, "no structured event emitted"
        assert all(p["stop_reason"] == "max_tokens" for p in payloads)
        assert all(p["output_tokens"] == 700 for p in payloads)


class TestSummarizerRecordsBothOutcomes:
    """The split on `aligned` is the measurement — both halves must land."""

    @staticmethod
    def _client(text):
        class _Block:
            type = "text"

            def __init__(self, t):
                self.text = t

        class _Msgs:
            async def create(self, **kw):
                r = _Response()
                r.content = [_Block(text)]
                return r

        class _Client:
            messages = _Msgs()

        return _Client()

    @pytest.mark.asyncio
    async def test_records_the_aligned_call(self):
        from app.models import RSSArticle
        from services.summarizer import _summarize_batch

        batch = [RSSArticle(title="T", source_name="S", source_url="u1",
                            raw_content="body text here")]
        good = '[{"i":1,"s":"A summary.","c":"technology"}]'

        calls = []
        with patch("services.summarizer.log_output_stop",
                   lambda *a, **kw: calls.append(kw)), \
             patch("services.summarizer.log_usage"):
            await _summarize_batch(self._client(good), batch)

        assert [c["aligned"] for c in calls] == [True]

    @pytest.mark.asyncio
    async def test_records_the_misaligned_call(self):
        """The regression that matters: recording only successes answers nothing."""
        from app.models import RSSArticle
        from services.summarizer import _summarize_batch

        batch = [RSSArticle(title="T", source_name="S", source_url="u1",
                            raw_content="body text here"),
                 RSSArticle(title="T2", source_name="S", source_url="u2",
                            raw_content="more body text")]
        # Truncated mid-array — one entry for a two-article batch.
        truncated = '[{"i":1,"s":"A summary.","c":"technology"}'

        calls = []
        with patch("services.summarizer.log_output_stop",
                   lambda *a, **kw: calls.append(kw)), \
             patch("services.summarizer.log_usage"), \
             pytest.raises(AlignmentError):
            await _summarize_batch(self._client(truncated), batch)

        assert [c["aligned"] for c in calls] == [False]
        assert calls[0]["batch_size"] == 2
