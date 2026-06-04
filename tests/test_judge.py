"""Tests for services/judge — the offline LLM-judge (sift-api#90).

Mocks the Anthropic client so we never hit the network. Verifies prompt
construction, response parsing + verdict derivation, rate tallying, and that
judge_lines aligns verdicts to inputs and marks unscored items as errors.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.judge import (
    _coerce_bool,
    _derive_verdict,
    _extract_json_array,
    _parse_judge,
    build_judge_prompt,
    judge_lines,
    tally,
)

ITEMS = [
    {"id": "a", "title": "Title A", "summary": "Summary A about a budget audit.", "line": "Restates the summary."},
    {"id": "b", "title": "Title B", "summary": "Summary B about a water deal.", "line": "Adds a concrete number."},
]


def _mock_client(text: str) -> AsyncMock:
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
    )
    return client


class TestBuildJudgePrompt:
    def test_contains_three_axes_and_items(self):
        prompt = build_judge_prompt(ITEMS)
        assert "restates" in prompt
        assert "adds_significance" in prompt
        assert "neutral_cliche_free" in prompt
        assert "Title A" in prompt
        assert "Summary B about a water deal." in prompt

    def test_background_framing_relaxes_restatement(self):
        prompt = build_judge_prompt(ITEMS, field="background")
        assert "do NOT treat" in prompt.lower() or "shared wording" in prompt.lower()


class TestCoerceBool:
    def test_variants(self):
        assert _coerce_bool(True) is True
        assert _coerce_bool("false") is False
        assert _coerce_bool("YES") is True
        assert _coerce_bool("0") is False
        assert _coerce_bool(None) is None
        assert _coerce_bool("maybe") is None


class TestDeriveVerdict:
    def test_pass_requires_all_three(self):
        assert _derive_verdict(False, True, True) == "pass"

    def test_restatement_fails(self):
        assert _derive_verdict(True, True, True) == "fail"

    def test_no_significance_fails(self):
        assert _derive_verdict(False, False, True) == "fail"

    def test_not_neutral_fails(self):
        assert _derive_verdict(False, True, False) == "fail"

    def test_unknown_axis_is_error(self):
        assert _derive_verdict(None, True, True) == "error"


class TestParseJudge:
    def test_parses_short_keys_and_derives_verdict(self):
        text = json.dumps([
            {"i": 1, "r": True, "a": False, "n": True, "why": "just rewords it"},
            {"i": 2, "r": False, "a": True, "n": True, "why": "adds a number"},
        ])
        out = _parse_judge(text, 2)
        assert out[1]["verdict"] == "fail"
        assert out[1]["restates"] is True
        assert out[2]["verdict"] == "pass"
        assert out[2]["reason"] == "adds a number"

    def test_out_of_range_index_ignored(self):
        text = json.dumps([{"i": 9, "r": False, "a": True, "n": True}])
        assert _parse_judge(text, 2) == {}

    def test_malformed_json_returns_empty(self):
        assert _parse_judge("not json at all", 2) == {}


class TestTally:
    def test_rates(self):
        verdicts = [
            {"judged": True, "verdict": "pass", "restates": False, "adds_significance": True, "neutral_cliche_free": True},
            {"judged": True, "verdict": "fail", "restates": True, "adds_significance": False, "neutral_cliche_free": True},
            {"judged": True, "verdict": "fail", "restates": False, "adds_significance": True, "neutral_cliche_free": False},
            {"judged": False, "verdict": "error"},
        ]
        t = tally(verdicts)
        assert t["n"] == 3
        assert t["errors"] == 1
        assert t["pass_rate"] == pytest.approx(1 / 3)
        assert t["restatement_rate"] == pytest.approx(1 / 3)
        assert t["cliche_or_editorial_rate"] == pytest.approx(1 / 3)
        assert t["adds_significance_rate"] == pytest.approx(2 / 3)

    def test_empty(self):
        t = tally([])
        assert t["n"] == 0
        assert t["pass_rate"] == 0.0


class TestJudgeLines:
    @pytest.mark.asyncio
    async def test_aligns_verdicts_to_items(self):
        text = json.dumps([
            {"i": 1, "r": True, "a": False, "n": True},
            {"i": 2, "r": False, "a": True, "n": True},
        ])
        verdicts = await judge_lines(ITEMS, client=_mock_client(text))
        assert len(verdicts) == 2
        assert verdicts[0]["id"] == "a"
        assert verdicts[0]["verdict"] == "fail"
        assert verdicts[1]["id"] == "b"
        assert verdicts[1]["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_unscored_item_marked_error(self):
        # Judge only returns a verdict for item 1; item 2 must surface as an error.
        text = json.dumps([{"i": 1, "r": False, "a": True, "n": True}])
        verdicts = await judge_lines(ITEMS, client=_mock_client(text))
        assert verdicts[1]["judged"] is False
        assert verdicts[1]["verdict"] == "error"

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits(self):
        client = _mock_client("[]")
        assert await judge_lines([], client=client) == []
        client.messages.create.assert_not_called()


class TestExtractJsonArray:
    def test_surrounding_prose(self):
        text = 'Here:\n[{"i": 1, "r": false}]\nDone.'
        assert _extract_json_array(text) == [{"i": 1, "r": False}]

    def test_invalid(self):
        assert _extract_json_array("nope") is None
