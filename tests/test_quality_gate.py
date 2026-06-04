"""Tests for services/quality_gate — the deterministic half of sift-api#90.

Pure functions, no network. The headline test is the corpus regression: every
labeled row in data/eval/why_it_matters_corpus.jsonl must gate to its
`expect_gate` value, which pins the two live production failures (cop-fired,
Kepner) as permanent negatives.
"""
from __future__ import annotations

import json
import os

from services.quality_gate import (
    NEAR_RESTATEMENT_MAX_NOVELTY,
    evaluate_background,
    evaluate_why_it_matters,
    find_cliche,
    gate_background,
    gate_why_it_matters,
    is_near_restatement,
    lexical_novelty,
)

CORPUS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "eval", "why_it_matters_corpus.jsonl",
)


def _load_corpus() -> list[dict]:
    with open(CORPUS_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


class TestCorpusRegression:
    def test_every_row_gates_as_expected(self):
        rows = _load_corpus()
        assert len(rows) >= 14  # guard against an accidentally-truncated corpus
        mismatches = []
        for r in rows:
            evaluate = evaluate_background if r["field"] == "background" else evaluate_why_it_matters
            res = evaluate(r["line"], title=r["title"], summary=r["summary"])
            actual = "drop" if res.dropped else "keep"
            if actual != r["expect_gate"]:
                mismatches.append((r["id"], r["expect_gate"], actual, res.reason))
        assert not mismatches, f"gate disagreed with corpus labels: {mismatches}"

    def test_cop_fired_dropped_on_cliche(self):
        # The live failure: ~36% novel (above any safe lexical threshold) but an
        # unmistakable cliché. Lexical overlap would miss it; the cliché catches it.
        row = next(r for r in _load_corpus() if r["id"] == "cop-fired")
        res = evaluate_why_it_matters(row["line"], title=row["title"], summary=row["summary"])
        assert res.dropped
        assert res.reason == "cliche"
        assert res.novelty > NEAR_RESTATEMENT_MAX_NOVELTY  # proves lexical gate alone fails

    def test_kepner_dropped_on_cliche(self):
        row = next(r for r in _load_corpus() if r["id"] == "kepner")
        res = evaluate_why_it_matters(row["line"], title=row["title"], summary=row["summary"])
        assert res.dropped
        assert res.reason == "cliche"

    def test_good_lines_survive(self):
        for r in _load_corpus():
            if r["label"] == "good" and r["field"] == "why_it_matters":
                assert gate_why_it_matters(r["line"], title=r["title"], summary=r["summary"]) is not None


class TestFindCliche:
    def test_vague_significance(self):
        assert find_cliche("This raises serious questions about oversight.")
        assert find_cliche("It remains to be seen how this plays out.")
        assert find_cliche("The deal marks a turning point for the industry.")
        assert find_cliche("A wake-up call for regulators.")

    def test_editorial_color(self):
        assert find_cliche("New York's most tortured fans finally have hope.")
        assert find_cliche("The case has haunted investigators for years.")

    def test_clean_line_has_no_cliche(self):
        assert find_cliche("The cuts hit farms growing a third of US winter lettuce.") is None
        assert find_cliche("Nvidia loses access to a quarter of its revenue.") is None


class TestNovelty:
    def test_pure_restatement_is_low_novelty(self):
        ref = "The Federal Reserve held interest rates steady, citing inflation."
        line = "The Federal Reserve held interest rates steady, citing inflation."
        assert lexical_novelty(line, ref) == 0.0
        assert is_near_restatement(line, ref)

    def test_novel_line_is_high_novelty(self):
        ref = "Seven states agreed to cut Colorado River water use."
        line = "Grocery prices could climb because the cuts hit major vegetable farms."
        assert lexical_novelty(line, ref) > 0.5
        assert not is_near_restatement(line, ref)

    def test_empty_line_zero_novelty(self):
        assert lexical_novelty("", "anything") == 0.0


class TestWhyItMattersGate:
    def test_empty_and_null_tokens_drop(self):
        for val in ["", "   ", "null", "None", "N/A", "-"]:
            assert gate_why_it_matters(val, title="t", summary="s") is None

    def test_strips_wrapping_quotes(self):
        kept = gate_why_it_matters(
            '"Grocery prices could climb because the cuts hit major farms."',
            title="Colorado River deal", summary="States agreed to water cuts.",
        )
        assert kept is not None
        assert not kept.startswith('"')

    def test_near_verbatim_restatement_drops(self):
        res = evaluate_why_it_matters(
            "The mayor announced a hiring freeze across all city departments.",
            title="Mayor orders hiring freeze",
            summary="The mayor announced a hiring freeze across all city departments.",
        )
        assert res.dropped
        assert res.reason == "restatement"


class TestBackgroundGate:
    def test_cliche_background_blanks_to_empty_string(self):
        # Drops to "" (not None) so the caller keeps `terms` and only hides the
        # paragraph.
        out = gate_background("This raises serious questions about the boom.")
        assert out == ""

    def test_clean_background_survives(self):
        bg = "The Colorado River supplies water to about 40 million people across seven states."
        assert gate_background(bg) == bg

    def test_long_paragraph_with_stray_cliche_is_kept(self):
        # Regression: a substantive multi-sentence paragraph whose closing clause
        # says "raising questions about <specific X>" must NOT be blanked — that
        # destroys real context. Cliché-blanking applies only to short paragraphs.
        bg = (
            "In 1982, President Reagan pressed Israel to halt its invasion of Lebanon after "
            "Israeli forces advanced toward Beirut. The standoff tested US-Israel relations "
            "during a Cold War proxy conflict, raising questions about how presidents balance "
            "support for Israel with regional stability."
        )
        assert gate_background(bg) == bg

    def test_background_not_dropped_for_restatement(self):
        # Unlike why_it_matters, background may reuse topic vocabulary — the
        # restatement backstop must NOT fire here.
        ref = "The Federal Reserve held interest rates steady."
        res = evaluate_background("The Federal Reserve held interest rates steady.", title="", summary=ref)
        assert not res.dropped
