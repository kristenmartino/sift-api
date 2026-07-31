"""Tests for services.story_clusterer.

This module had no tests at all, while `sift/docs/DECISIONS.md` called it "the
core differentiator" and claimed ~97% event-level clustering accuracy. These
cover the deterministic half — parsing, index validation, and the output
ceiling. Accuracy against labeled ground truth is a separate concern; see
services/cluster_metrics.py and scripts/eval_clustering.py.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.story_clusterer import (
    _extract_json_array,
    _max_tokens_for,
    _parse_clusters,
    cluster_articles,
)


def _mock_client(text: str, *, stop_reason: str = "end_turn") -> AsyncMock:
    """Replay a fixed response. Mirrors the pattern in tests/test_judge.py."""
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


def _articles(n: int) -> list[dict]:
    return [
        {
            "source_url": f"https://example.com/{i}",
            "source_name": f"Outlet{i}",
            "title": f"Title {i}",
            "summary": f"Summary {i}",
            "entities": {},
        }
        for i in range(1, n + 1)
    ]


class TestMaxTokensCeiling:
    """The regression that produced zero stories with no error logged.

    A fixed max_tokens=1024 truncated the JSON array on large windows, which
    failed _extract_json_array -> _parse_clusters returned [] -> the whole
    category silently produced no stories.
    """

    def test_scales_with_article_count(self):
        assert _max_tokens_for(2) < _max_tokens_for(50)

    def test_large_window_gets_more_than_the_old_fixed_ceiling(self):
        # The old hardcoded value was 1024 and this is precisely the case that
        # overflowed it.
        assert _max_tokens_for(50) > 1024

    def test_capped_so_a_runaway_window_cannot_blow_up_cost(self):
        assert _max_tokens_for(10_000) == 2048

    def test_small_window_is_not_starved(self):
        # Two articles still need room for the array scaffolding plus a group.
        assert _max_tokens_for(2) >= 128


class TestParseClusters:
    def test_rejects_single_article_groups(self):
        # The product contract is cross-outlet comparison; a 1-article "group"
        # is never a story.
        out = _parse_clusters('[{"group_id": 1, "article_indices": [1], "event": "x"}]', 5)
        assert out == []

    def test_rejects_out_of_range_indices(self):
        out = _parse_clusters('[{"group_id": 1, "article_indices": [1, 99], "event": "x"}]', 5)
        assert out == []

    def test_rejects_zero_index_because_numbering_is_1_based(self):
        out = _parse_clusters('[{"group_id": 1, "article_indices": [0, 1], "event": "x"}]', 5)
        assert out == []

    def test_rejects_non_integer_indices(self):
        out = _parse_clusters('[{"group_id": 1, "article_indices": ["1", 2], "event": "x"}]', 5)
        assert out == []

    def test_accepts_a_valid_group(self):
        out = _parse_clusters('[{"group_id": 1, "article_indices": [1, 3], "event": "vote"}]', 5)
        assert out == [{"group_id": 1, "article_indices": [1, 3], "event": "vote"}]

    def test_keeps_valid_groups_and_drops_invalid_ones_in_the_same_response(self):
        text = (
            '[{"group_id": 1, "article_indices": [1, 2], "event": "ok"},'
            ' {"group_id": 2, "article_indices": [3, 99], "event": "bad"}]'
        )
        out = _parse_clusters(text, 5)
        assert len(out) == 1
        assert out[0]["event"] == "ok"

    def test_truncated_json_yields_no_clusters(self):
        # Exactly what a max_tokens cut-off looks like on the wire.
        truncated = '[{"group_id": 1, "article_indices": [1, 2], "event": "part'
        assert _parse_clusters(truncated, 5) == []


class TestExtractJsonArray:
    def test_bare_array(self):
        assert _extract_json_array('[{"a": 1}]') == [{"a": 1}]

    def test_array_wrapped_in_prose(self):
        assert _extract_json_array('Here you go:\n[{"a": 1}]\nHope that helps') == [{"a": 1}]

    def test_returns_none_on_unparseable_text(self):
        assert _extract_json_array("no json here") is None

    def test_returns_none_on_a_json_object_rather_than_an_array(self):
        assert _extract_json_array('{"a": 1}') is None


class TestClusterArticles:
    @pytest.mark.asyncio
    async def test_short_circuits_below_two_articles_without_calling_the_api(self):
        client = _mock_client("[]")
        assert await cluster_articles(_articles(1), client=client) == []
        client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_injected_client_is_used_instead_of_a_live_one(self):
        # This kwarg is what makes a free, deterministic replay eval possible.
        client = _mock_client('[{"group_id": 1, "article_indices": [1, 2], "event": "vote"}]')
        out = await cluster_articles(_articles(4), client=client)
        client.messages.create.assert_called_once()
        assert out[0]["article_indices"] == [1, 2]

    @pytest.mark.asyncio
    async def test_output_ceiling_is_passed_through_to_the_api_call(self):
        client = _mock_client("[]")
        await cluster_articles(_articles(50), client=client)
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == _max_tokens_for(50)

    @pytest.mark.asyncio
    async def test_api_error_degrades_to_no_clusters(self):
        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        assert await cluster_articles(_articles(3), client=client) == []

    @pytest.mark.asyncio
    async def test_truncation_is_logged_rather_than_silent(self, caplog):
        """The whole point of the fix: a truncated response must be visible."""
        client = _mock_client(
            '[{"group_id": 1, "article_indices": [1, 2], "event": "part',
            stop_reason="max_tokens",
        )
        with caplog.at_level(logging.INFO, logger="sift-api.story_clusterer"):
            out = await cluster_articles(_articles(50), client=client)

        assert out == []
        stats = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{") and '"cluster_stats"' in r.message
        ]
        assert stats, "no cluster_stats event was emitted"
        assert stats[0]["stop_reason"] == "max_tokens"
        assert stats[0]["n_groups"] == 0
        # And a human-readable warning, so this shows up without log parsing.
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_empty_result_is_distinguishable_from_truncation(self):
        """A genuinely empty response and a truncated one both return [] — the
        structured log is the only thing that tells them apart."""
        client = _mock_client("[]", stop_reason="end_turn")
        out = await cluster_articles(_articles(10), client=client)
        assert out == []
