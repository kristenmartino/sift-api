from __future__ import annotations

import json

from services.summarizer import (
    _extract_json_array,
    _build_prompt,
    _parse_summaries,
    _truncate,
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
    def test_valid_json_response(self):
        batch = [_make_article(1), _make_article(2)]
        text = json.dumps([
            {"index": 1, "summary": "Summary one"},
            {"index": 2, "summary": "Summary two"},
        ])
        results = _parse_summaries(text, batch)
        assert results["https://example.com/article-1"] == "Summary one"
        assert results["https://example.com/article-2"] == "Summary two"

    def test_out_of_range_index_ignored(self):
        batch = [_make_article(1)]
        text = json.dumps([
            {"index": 1, "summary": "Good"},
            {"index": 5, "summary": "Out of range"},
        ])
        results = _parse_summaries(text, batch)
        assert len(results) == 1
        assert "https://example.com/article-1" in results

    def test_fallback_to_lines(self):
        batch = [_make_article(1), _make_article(2)]
        text = "1. This is summary one\n2. This is summary two"
        results = _parse_summaries(text, batch)
        assert len(results) == 2

    def test_empty_text(self):
        batch = [_make_article(1)]
        text = ""
        results = _parse_summaries(text, batch)
        assert results == {}


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
