"""Tests for the deploy check in scripts/verify_cost_baseline.py.

WHY THIS FILE EXISTS
--------------------
The check had no tests, and on 2026-08-10 it was found to be one merge away
from reporting `NOT DEPLOYED` at the exact moment incremental threading (#161)
finished cutting over: it divided synthesize calls by `story_clusterer.cluster`
calls, and the cutover drives that denominator to zero.

`0 < 0 < 3.0` is false, so a perfect cutover looked identical to a missing
deploy — and the script exits non-zero on that verdict, so the scheduled run
would have raised an alarm about the thing working. The regression test is
`test_incremental_path_passes_with_clusterer_dead`.
"""
import importlib.util
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_cost_baseline",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "verify_cost_baseline.py"),
)
vcb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vcb)


GATED_LINKER = {"entity_linker_llm.link_text": 400.0}  # 0.24/article at 1,672


def _calls(**kw) -> dict[str, float]:
    return {**GATED_LINKER, **kw}


class TestPathDetection:
    def test_clusterer_only_is_legacy(self):
        _, _, path = vcb.deploy_check(
            _calls(**{"story_clusterer.cluster": 218.0,
                      "story_synthesizer.synthesize": 293.0}), 1672.0, 300.0, 0.0)
        assert path == "legacy"

    def test_confirmer_only_is_incremental(self):
        _, _, path = vcb.deploy_check(
            _calls(**{"story_confirmer.confirm": 48.0,
                      "story_synthesizer.synthesize": 120.0}), 1672.0, 300.0, 2000.0)
        assert path == "incremental"

    def test_both_is_mixed(self):
        _, _, path = vcb.deploy_check(
            _calls(**{"story_clusterer.cluster": 200.0,
                      "story_confirmer.confirm": 69.0,
                      "story_synthesizer.synthesize": 621.0}), 1776.0, 400.0, 900.0)
        assert path == "mixed"


class TestShadowModeIsNotACutover:
    """Prod's 2026-08-08 shape, which broke the first version of this fix.

    Shadow mode bills `story_confirmer.confirm` (20/45/45 calls on
    08-07/08/09) while the legacy path is the live one. Keying "incremental
    is live" off confirmer calls reported both paths running on a day when
    only one was. `threaded_at` is the discriminator: `mark_threaded` is
    reached only from `run_incremental_threading`.
    """

    SHADOW_DAY = {
        "story_clusterer.cluster": 218.0,
        "story_confirmer.confirm": 45.0,
        "story_synthesizer.synthesize": 284.0,
    }

    def test_shadow_confirmer_does_not_read_as_incremental(self):
        _, _, path = vcb.deploy_check(_calls(**self.SHADOW_DAY), 1672.0, 300.0, 0.0)
        assert path == "legacy"

    def test_shadow_day_still_gets_a_real_legacy_verdict(self):
        """The bug cost a verdict, not just a label: 08-08 went unjudged."""
        _, verdicts, _ = vcb.deploy_check(_calls(**self.SHADOW_DAY), 1672.0, 300.0, 0.0)
        assert verdicts["synthesis_reuse_live"] is True

    def test_shadow_spend_is_named(self):
        lines, _, _ = vcb.deploy_check(_calls(**self.SHADOW_DAY), 1672.0, 300.0, 0.0)
        assert "SHADOW" in " ".join(n for _, n in lines)


class TestIncrementalPath:
    def test_incremental_path_passes_with_clusterer_dead(self):
        """The regression. Clusterer at zero is success, not a missing deploy.

        Before the fix this returned synthesis_reuse_live=False and the script
        exited 2.
        """
        _, verdicts, _ = vcb.deploy_check(
            _calls(**{"story_confirmer.confirm": 48.0,
                      "story_synthesizer.synthesize": 120.0}), 1672.0, 300.0, 2000.0)
        assert verdicts["legacy_threading_retired"] is True
        assert verdicts["synthesis_reuse_live"] is True
        assert all(verdicts.values())

    def test_every_touch_resynthesizing_fails(self):
        """Reuse not holding: one synthesis per touched story, sustained."""
        _, verdicts, _ = vcb.deploy_check(
            _calls(**{"story_confirmer.confirm": 48.0,
                      "story_synthesizer.synthesize": 900.0}), 1672.0, 300.0, 2000.0)
        assert verdicts["synthesis_reuse_live"] is False

    def test_no_stories_touched_does_not_divide_by_zero(self):
        _, verdicts, _ = vcb.deploy_check(
            _calls(**{"story_confirmer.confirm": 48.0,
                      "story_synthesizer.synthesize": 120.0}), 1672.0, 0.0, 2000.0)
        assert verdicts["synthesis_reuse_live"] is False


class TestMixedWindow:
    def test_mixed_issues_no_threading_verdict(self):
        """2026-08-10's own shape: 13h clusterer + 5.5h confirmer.

        The blended ratio is meaningless, so the honest output is silence on
        threading — not a pass and not a failure.
        """
        _, verdicts, _ = vcb.deploy_check(
            _calls(**{"story_clusterer.cluster": 200.0,
                      "story_confirmer.confirm": 69.0,
                      "story_synthesizer.synthesize": 621.0}), 1776.0, 400.0, 900.0)
        assert "synthesis_reuse_live" not in verdicts
        assert "legacy_threading_retired" not in verdicts

    def test_mixed_names_the_double_billing_reading(self):
        """The expensive reading must not be swallowed by "can't tell".

        A cutover-spanning window and both-paths-billing produce the same
        cl/cf ratio at any blend, so the check cannot separate them. It must
        therefore surface both, and name the resolving fact — otherwise the
        costliest fault hides behind an indeterminate verdict.
        """
        lines, _, _ = vcb.deploy_check(
            _calls(**{"story_clusterer.cluster": 200.0,
                      "story_confirmer.confirm": 69.0,
                      "story_synthesizer.synthesize": 621.0}), 1776.0, 400.0, 900.0)
        note = " ".join(n for _, n in lines)
        assert "both paths are billing" in note
        assert "2026-08-10" in note

    def test_mixed_still_judges_the_linker(self):
        """The linker gate is independent of threading and stays checkable."""
        _, verdicts, _ = vcb.deploy_check(
            {"entity_linker_llm.link_text": 1650.0,
             "story_clusterer.cluster": 200.0,
             "story_confirmer.confirm": 69.0}, 1672.0, 400.0, 900.0)
        assert verdicts["regex_gate_live"] is False


class TestLegacyPathUnchanged:
    @pytest.mark.parametrize(("syn", "cl", "expected"), [
        (293.0, 218.0, True),    # 1.34 — reuse skip live
        (959.0, 218.0, False),   # 4.40 — the pre-#129 figure
    ])
    def test_legacy_ratio_bar_preserved(self, syn, cl, expected):
        _, verdicts, _ = vcb.deploy_check(
            _calls(**{"story_clusterer.cluster": cl,
                      "story_synthesizer.synthesize": syn}), 1672.0, 300.0, 0.0)
        assert verdicts["synthesis_reuse_live"] is expected

    def test_no_threading_data_at_all_fails_rather_than_passes(self):
        """An empty ledger must not read as a pass."""
        _, verdicts, path = vcb.deploy_check(GATED_LINKER, 1672.0, 300.0, 0.0)
        assert path == "legacy"
        assert verdicts["synthesis_reuse_live"] is False


class TestLinkerGate:
    @pytest.mark.parametrize(("linker_calls", "expected"), [
        (400.0, True),    # 0.24/article — gated
        (1672.0, False),  # 1.00/article — ungated
    ])
    def test_gate_verdict(self, linker_calls, expected):
        _, verdicts, _ = vcb.deploy_check(
            {"entity_linker_llm.link_text": linker_calls,
             "story_confirmer.confirm": 48.0,
             "story_synthesizer.synthesize": 120.0}, 1672.0, 300.0, 2000.0)
        assert verdicts["regex_gate_live"] is expected

    def test_zero_articles_does_not_divide_by_zero(self):
        _, verdicts, _ = vcb.deploy_check({"entity_linker_llm.link_text": 400.0}, 0.0, 0.0, 0.0)
        assert verdicts["regex_gate_live"] is True
