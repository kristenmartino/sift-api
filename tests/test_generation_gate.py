"""Tests for the quality-gate integration in the generation parse paths (#90).

Covers the behavior changes in context_generator._parse_context (gate + the
score/line decouple) and primer_generator._parse_primers (background cliché
gate that keeps `terms`). Pure functions — no network.
"""
from __future__ import annotations

import json

from services.context_generator import _build_context_prompt, _parse_context
from services.primer_generator import _parse_primers

CTX_BATCH = [{
    "source_url": "https://example.com/1",
    "title": "Seven states reach Colorado River water deal",
    "summary": "Seven Western states agreed to reduce Colorado River water allocations.",
}]

GOOD_LINE = "The cuts hit farms that grow most of America's winter lettuce and broccoli."


class TestContextParse:
    def test_good_line_kept_with_score(self):
        text = json.dumps([{"i": 1, "c": GOOD_LINE, "s": 4}])
        out = _parse_context(text, CTX_BATCH)
        assert out["https://example.com/1"]["context"] == GOOD_LINE
        assert out["https://example.com/1"]["score"] == 4

    def test_cliche_line_dropped_but_score_recorded(self):
        # The decouple: a gated line must NOT cost us the importance score.
        text = json.dumps([{"i": 1, "c": "This raises serious questions about water policy.", "s": 3}])
        out = _parse_context(text, CTX_BATCH)
        assert out["https://example.com/1"]["context"] is None
        assert out["https://example.com/1"]["score"] == 3

    def test_empty_line_still_records_score(self):
        text = json.dumps([{"i": 1, "c": "", "s": 5}])
        out = _parse_context(text, CTX_BATCH)
        assert out["https://example.com/1"]["context"] is None
        assert out["https://example.com/1"]["score"] == 5

    def test_bad_score_clamped(self):
        text = json.dumps([{"i": 1, "c": GOOD_LINE, "s": 99}])
        out = _parse_context(text, CTX_BATCH)
        assert out["https://example.com/1"]["score"] == 3


class TestContextPrompt:
    def test_prompt_encodes_the_rubric(self):
        prompt = _build_context_prompt(CTX_BATCH).lower()
        assert "verifiable" in prompt
        assert "empty string" in prompt          # the null-over-filler instruction
        assert "do not restate" in prompt
        assert "importance score" in prompt       # score still requested
        assert "raises serious questions" in prompt  # banned-phrasing example


class TestPrimerParse:
    def _batch(self):
        return [{
            "source_url": "https://example.com/p1",
            "title": "AI startups raise record funding",
            "summary": "Venture funding for AI startups hit a record this quarter.",
        }]

    def test_cliche_background_blanked_terms_kept(self):
        text = json.dumps([{
            "i": 1,
            "b": "The surge raises serious questions about whether this is a turning point.",
            "t": [{"term": "down round", "def": "A funding round at a lower valuation than the prior one."}],
        }])
        out = _parse_primers(text, self._batch())
        rec = out["https://example.com/p1"]
        assert rec["background"] == ""                  # cliché dropped
        assert len(rec["terms"]) == 1                    # terms preserved
        assert rec["terms"][0]["term"] == "down round"

    def test_clean_background_kept(self):
        bg = "Venture firms raise money from investors and deploy it into startups over a fund's ten-year life."
        text = json.dumps([{"i": 1, "b": bg, "t": []}])
        out = _parse_primers(text, self._batch())
        assert out["https://example.com/p1"]["background"] == bg
