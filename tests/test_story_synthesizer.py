"""Tests for services.story_synthesizer.

The module had no tests. It acquired these when a fixed `max_tokens=1024` was
found truncating synthesis on exactly the stories the product exists to show:
`framings` carries one entry per source, so output grows with the cluster, and
past roughly 13 outlets every call was cut off mid-JSON. `_extract_json_object`
then returned None and the call degraded to `_fallback()` — the first article's
own headline — which #210 had just stopped storing as 'complete'.

Measured against prod 2026-08-11, five stories a fixed 1024 was failing:
`stop_reason='max_tokens'` on four of five, and all five parsed cleanly at
4096. Worst case 1,348 output tokens for 24 articles across 18 outlets.

Mirrors the ceiling and truncation-visibility tests story_clusterer gained in
#113 for the identical bug.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.story_synthesizer import (
    _extract_json_object,
    _fallback,
    _max_tokens_for,
    synthesize_story,
)

_GOOD = json.dumps({
    "headline": "H", "summary": "S",
    "framings": [{"source_name": "Outlet1", "framing": "f", "tone": "neutral"}],
})


def _mock_client(text: str, *, stop_reason: str = "end_turn") -> AsyncMock:
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason,
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
    )
    return client


def _articles(n: int, outlets: int | None = None) -> list[dict]:
    outlets = outlets or n
    return [
        {
            "source_url": f"https://example.com/{i}",
            "source_name": f"Outlet{i % outlets}",
            "title": f"Title {i}",
            "summary": f"Summary {i}",
        }
        for i in range(1, n + 1)
    ]


class TestMaxTokensCeiling:
    """The regression: a fixed ceiling breaks on the biggest stories only.

    `framings` is one entry per source, so a story that grows past ~13 outlets
    truncates — and `_attach` re-synthesizes precisely as outlets accumulate,
    so the failure is permanent for that story rather than intermittent.
    """

    def test_scales_with_article_count(self):
        assert _max_tokens_for(2) < _max_tokens_for(24)

    def test_the_measured_failing_cases_clear_their_observed_output(self):
        # 24 articles / 18 outlets produced 1,348 output tokens; 21 articles
        # produced 1,288. Both were cut off at the old fixed 1024.
        assert _max_tokens_for(24) > 1348
        assert _max_tokens_for(21) > 1288

    def test_no_cluster_gets_less_room_than_the_old_fixed_ceiling(self):
        # The floor exists so this fix cannot regress a small story.
        assert _max_tokens_for(2) >= 1024
        assert _max_tokens_for(0) >= 1024

    def test_capped_so_a_runaway_cluster_cannot_blow_up_the_call(self):
        assert _max_tokens_for(10_000) == 8192


class TestSynthesizeStory:
    @pytest.mark.asyncio
    async def test_short_circuits_below_two_articles_without_calling_the_api(self):
        client = _mock_client(_GOOD)
        out = await synthesize_story(_articles(1), client=client)
        client.messages.create.assert_not_called()
        assert out["_failed"] is True

    @pytest.mark.asyncio
    async def test_scaled_ceiling_reaches_the_api_call(self):
        client = _mock_client(_GOOD)
        await synthesize_story(_articles(24, outlets=18), client=client)
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == _max_tokens_for(24)
        assert kwargs["max_tokens"] > 1024

    @pytest.mark.asyncio
    async def test_truncation_is_logged_rather_than_silent(self, caplog):
        """The reason this went a day misread as API flakiness: a cut-off
        response and an unparseable one logged the same line."""
        truncated = '{"headline": "H", "summary": "S", "framings": [{"source_'
        client = _mock_client(truncated, stop_reason="max_tokens")

        with caplog.at_level(logging.INFO, logger="sift-api.story_synthesizer"):
            out = await synthesize_story(_articles(24, outlets=18), client=client)

        assert out["_failed"] is True
        stats = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{") and '"synthesis_stats"' in r.message
        ]
        assert stats, "no synthesis_stats event was emitted"
        assert stats[0]["stop_reason"] == "max_tokens"
        assert stats[0]["parsed"] is False
        assert stats[0]["n_outlets"] == 18
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_complete_response_is_returned_without_the_failed_flag(self):
        client = _mock_client(_GOOD)
        out = await synthesize_story(_articles(4), client=client)
        assert "_failed" not in out
        assert out["headline"] == "H"

    @pytest.mark.asyncio
    async def test_an_unrecognised_tone_is_clamped_to_neutral(self):
        text = json.dumps({
            "headline": "H", "summary": "S",
            "framings": [{"source_name": "O", "framing": "f", "tone": "furious"}],
        })
        out = await synthesize_story(_articles(4), client=_mock_client(text))
        assert out["framings"][0]["tone"] == "neutral"

    @pytest.mark.asyncio
    async def test_api_error_degrades_to_a_flagged_fallback(self):
        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        out = await synthesize_story(_articles(3), client=client)
        assert out["_failed"] is True


class TestFallback:
    def test_carries_the_failed_flag_so_callers_can_refuse_to_store_it(self):
        """`incremental_threading` and `story_workflow.py:246` both branch on
        this. Without it a placeholder is indistinguishable from a synthesis."""
        assert _fallback(_articles(3))["_failed"] is True
        assert _fallback([])["_failed"] is True

    def test_uses_the_first_articles_text(self):
        out = _fallback(_articles(3))
        assert out["headline"] == "Title 1"
        assert out["framings"] == []


class TestExtractJsonObject:
    def test_bare_object(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_object_wrapped_in_prose(self):
        assert _extract_json_object('Sure:\n{"a": 1}\ndone') == {"a": 1}

    def test_returns_none_on_a_truncated_object(self):
        # Exactly what a max_tokens cut-off looks like on the wire.
        assert _extract_json_object('{"headline": "H", "framings": [{"sou') is None

    def test_returns_none_on_unparseable_text(self):
        assert _extract_json_object("no json here") is None
