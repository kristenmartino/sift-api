from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.index_alignment import MAX_BATCH_ATTEMPTS, AlignmentError
from services.summarizer import (
    _extract_json_array,
    _build_prompt,
    _parse_summaries,
    _truncate,
    summarize_articles,
)
from app.models import RSSArticle


def _make_article(idx: int) -> RSSArticle:
    return RSSArticle(
        title=f"Article {idx}",
        source_url=f"https://example.com/article-{idx}",
        source_name="TestSource",
        category="technology",
        raw_content=f"This is the raw content of article {idx}.",
    )


def _mock_client(*texts: str) -> AsyncMock:
    """Replay one response per call. Mirrors tests/test_story_clusterer.py."""
    client = AsyncMock()
    client.messages.create = AsyncMock(
        side_effect=[
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
            )
            for text in texts
        ]
    )
    return client


def _response(*items: dict) -> str:
    return json.dumps(list(items))


class TestExtractJsonArray:
    def test_clean_json(self):
        text = '[{"index": 1, "summary": "Hello"}]'
        result = _extract_json_array(text)
        assert result == [{"index": 1, "summary": "Hello"}]

    def test_json_with_surrounding_text(self):
        text = 'Here is the result:\n[{"index": 1, "summary": "Hello"}]\nDone.'
        result = _extract_json_array(text)
        assert result == [{"index": 1, "summary": "Hello"}]

    def test_individual_objects(self):
        text = 'Summary 1: {"index": 1, "summary": "First"}\nSummary 2: {"index": 2, "summary": "Second"}'
        result = _extract_json_array(text)
        assert len(result) == 2
        assert result[0]["summary"] == "First"
        assert result[1]["summary"] == "Second"

    def test_completely_invalid(self):
        result = _extract_json_array("This is not JSON at all")
        assert result is None

    def test_empty_array(self):
        result = _extract_json_array("[]")
        assert result == []

    def test_markdown_code_block(self):
        text = '```json\n[{"index": 1, "summary": "Test"}]\n```'
        result = _extract_json_array(text)
        assert result is not None
        assert result[0]["summary"] == "Test"


class TestParseSummaries:
    """The response's own indices are the ONLY thing tying a summary to an
    article, so anything short of a complete, duplicate-free {1..n} is
    rejected rather than written. Range-checking alone let production
    articles carry other articles' summaries (found 2026-07-30)."""

    def test_valid_json_response(self):
        batch = [_make_article(1), _make_article(2)]
        text = _response(
            {"index": 1, "summary": "Summary one", "category": "technology"},
            {"index": 2, "summary": "Summary two", "category": "business"},
        )
        results = _parse_summaries(text, batch)
        assert results["https://example.com/article-1"]["summary"] == "Summary one"
        assert results["https://example.com/article-1"]["category"] == "technology"
        assert results["https://example.com/article-2"]["summary"] == "Summary two"
        assert results["https://example.com/article-2"]["category"] == "business"

    def test_short_keys_are_accepted(self):
        batch = [_make_article(1)]
        results = _parse_summaries(_response({"i": 1, "s": "Short keys", "c": "health"}), batch)
        assert results["https://example.com/article-1"] == {
            "summary": "Short keys",
            "category": "health",
        }

    def test_out_of_order_response_maps_by_index_not_position(self):
        # Reordering is legal — index, not position, carries the mapping.
        batch = [_make_article(1), _make_article(2), _make_article(3)]
        text = _response(
            {"i": 3, "s": "Third", "c": "top"},
            {"i": 1, "s": "First", "c": "top"},
            {"i": 2, "s": "Second", "c": "top"},
        )
        results = _parse_summaries(text, batch)
        assert results["https://example.com/article-1"]["summary"] == "First"
        assert results["https://example.com/article-2"]["summary"] == "Second"
        assert results["https://example.com/article-3"]["summary"] == "Third"

    def test_duplicate_index_rejects_the_whole_batch(self):
        # The signature of the production bug: index 1 twice means article 2
        # has no summary of its own, and one of the two is somebody else's.
        batch = [_make_article(1), _make_article(2)]
        text = _response(
            {"i": 1, "s": "Summary one", "c": "top"},
            {"i": 1, "s": "Summary two", "c": "top"},
        )
        with pytest.raises(AlignmentError, match="duplicate index 1"):
            _parse_summaries(text, batch)

    def test_missing_index_rejects_the_whole_batch(self):
        # A skipped index is indistinguishable from a shift; the entries that
        # ARE present cannot be trusted either, so nothing is kept.
        batch = [_make_article(1), _make_article(2), _make_article(3)]
        text = _response(
            {"i": 1, "s": "First", "c": "top"},
            {"i": 3, "s": "Third", "c": "top"},
        )
        with pytest.raises(AlignmentError, match=r"missing indices \[2\]"):
            _parse_summaries(text, batch)

    def test_more_items_than_articles_rejects_the_whole_batch(self):
        batch = [_make_article(1)]
        text = _response(
            {"index": 1, "summary": "Good"},
            {"index": 5, "summary": "Out of range"},
        )
        with pytest.raises(AlignmentError, match="index 5 outside 1..1"):
            _parse_summaries(text, batch)

    def test_zero_index_rejected_because_numbering_is_1_based(self):
        batch = [_make_article(1)]
        with pytest.raises(AlignmentError, match="outside"):
            _parse_summaries(_response({"i": 0, "s": "Zero", "c": "top"}), batch)

    def test_non_integer_index_rejected(self):
        batch = [_make_article(1)]
        with pytest.raises(AlignmentError, match="non-integer index"):
            _parse_summaries(_response({"i": "1", "s": "Stringly typed", "c": "top"}), batch)

    def test_empty_summary_counts_as_a_missing_entry(self):
        batch = [_make_article(1), _make_article(2)]
        text = _response(
            {"i": 1, "s": "  ", "c": "top"},
            {"i": 2, "s": "Second", "c": "top"},
        )
        with pytest.raises(AlignmentError, match="empty summary at index 1"):
            _parse_summaries(text, batch)

    def test_unknown_category_is_coerced_not_treated_as_misalignment(self):
        batch = [_make_article(1)]
        results = _parse_summaries(_response({"i": 1, "s": "Body", "c": "sportsball"}), batch)
        assert results["https://example.com/article-1"]["category"] == "top"

    def test_preamble_line_no_longer_shifts_every_summary(self):
        """The deleted positional fallback's failure mode.

        One extra leading line used to shift the whole batch by one — every
        article got its neighbour's summary. Unparseable output must now
        produce nothing at all.
        """
        batch = [_make_article(1), _make_article(2)]
        text = "Here are the summaries:\n1. This is summary one\n2. This is summary two"
        with pytest.raises(AlignmentError, match="not a parseable JSON array"):
            _parse_summaries(text, batch)

    def test_plain_text_lines_are_never_mapped_by_position(self):
        batch = [_make_article(1), _make_article(2)]
        text = "1. This is summary one\n2. This is summary two"
        with pytest.raises(AlignmentError, match="not a parseable JSON array"):
            _parse_summaries(text, batch)

    def test_empty_text(self):
        batch = [_make_article(1)]
        with pytest.raises(AlignmentError, match="not a parseable JSON array"):
            _parse_summaries("", batch)

    def test_empty_array_for_a_non_empty_batch_is_rejected(self):
        batch = [_make_article(1)]
        with pytest.raises(AlignmentError, match="missing indices"):
            _parse_summaries("[]", batch)


class TestSummarizeArticlesAlignment:
    """End-to-end over the retry/fallback path with a replayed client."""

    @pytest.mark.asyncio
    async def test_misaligned_batch_is_retried_and_the_retry_is_used(self):
        batch = [_make_article(1), _make_article(2)]
        client = _mock_client(
            _response(  # index 1 twice — article 2 would have been mis-summarized
                {"i": 1, "s": "Wrong one", "c": "top"},
                {"i": 1, "s": "Wrong two", "c": "top"},
            ),
            _response(
                {"i": 1, "s": "Right one", "c": "technology"},
                {"i": 2, "s": "Right two", "c": "business"},
            ),
        )
        results = await summarize_articles(batch, client=client)
        assert client.messages.create.await_count == 2
        assert results["https://example.com/article-1"]["summary"] == "Right one"
        assert results["https://example.com/article-2"]["summary"] == "Right two"

    @pytest.mark.asyncio
    async def test_persistent_misalignment_never_writes_a_model_summary(self):
        batch = [_make_article(1), _make_article(2)]
        shifted = _response(
            {"i": 2, "s": "Summary that belongs to another article", "c": "top"},
        )
        client = _mock_client(*([shifted] * MAX_BATCH_ATTEMPTS))
        results = await summarize_articles(batch, client=client)

        assert client.messages.create.await_count == MAX_BATCH_ATTEMPTS
        # Degraded to each article's OWN raw content — crude, but it cannot
        # carry another article's text.
        for article in batch:
            summary = results[article.source_url]["summary"]
            assert summary == article.raw_content
            assert "belongs to another article" not in summary

    @pytest.mark.asyncio
    async def test_misalignment_is_logged_with_the_batch_source_urls(self, caplog):
        batch = [_make_article(1), _make_article(2)]
        bad = _response({"i": 1, "s": "Only one of two", "c": "top"})
        client = _mock_client(*([bad] * MAX_BATCH_ATTEMPTS))

        with caplog.at_level(logging.INFO, logger="sift-api.summarizer"):
            await summarize_articles(batch, client=client)

        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{") and '"summary_batch_misaligned"' in r.message
        ]
        assert len(events) == MAX_BATCH_ATTEMPTS
        assert events[0]["source_urls"] == [a.source_url for a in batch]
        assert events[0]["batch_index"] == 0
        assert events[-1]["final"] is True
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_aligned_batch_costs_exactly_one_call(self):
        batch = [_make_article(1)]
        client = _mock_client(_response({"i": 1, "s": "Fine", "c": "top"}))
        results = await summarize_articles(batch, client=client)
        client.messages.create.assert_awaited_once()
        assert results["https://example.com/article-1"]["summary"] == "Fine"

    @pytest.mark.asyncio
    async def test_api_error_degrades_to_raw_content_without_retrying(self):
        batch = [_make_article(1)]
        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        results = await summarize_articles(batch, client=client)
        # Transport retries belong to the SDK client, not to this loop.
        assert client.messages.create.await_count == 1
        assert results["https://example.com/article-1"]["summary"] == batch[0].raw_content

    @pytest.mark.asyncio
    async def test_no_articles_makes_no_call(self):
        client = _mock_client()
        assert await summarize_articles([], client=client) == {}
        client.messages.create.assert_not_called()


class TestBuildPrompt:
    def test_prompt_contains_titles(self):
        batch = [_make_article(1), _make_article(2)]
        prompt = _build_prompt(batch)
        assert "Article 1" in prompt
        assert "Article 2" in prompt
        assert "JSON array" in prompt

    def test_html_stripped(self):
        article = RSSArticle(
            title="Test",
            source_url="https://example.com/1",
            source_name="Test",
            category="technology",
            raw_content="<p>Hello <b>world</b></p>",
        )
        prompt = _build_prompt([article])
        assert "<p>" not in prompt
        assert "<b>" not in prompt
        assert "Hello world" in prompt


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello world", 10) == "hello world"

    def test_long_text_truncated(self):
        text = " ".join(f"word{i}" for i in range(100))
        result = _truncate(text, 5)
        assert result.endswith("...")
        # "..." is appended to the 5th word: "word0 word1 word2 word3 word4..."
        assert len(result.split()) == 5

    def test_exact_limit(self):
        text = "one two three"
        assert _truncate(text, 3) == "one two three"
