"""The eleven measured figures, and the guarantee that centralizing them changed nothing.

`services/cost_estimates.py` replaced eleven hardcoded USD constants across
eight files. The constants are gone, so nothing but this file records what they
were — and a refactor of the numbers feeding a fail-closed budget ceiling has
to prove it moved none of them.

The asymmetry is why it matters. Too low and the guard authorizes a run that
blows the ceiling; it is the only backstop and it fails in the expensive
direction. Too high and the guard blocks a run that would have fit, and the
summarizer degrades to truncated RSS text that nothing revisits — a quality
regression presenting as a budget event.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from services import cost_estimates, model_registry
from services.cost_estimates import BASELINE, estimate_cost

REPO_ROOT = pathlib.Path(__file__).parent.parent

# Transcribed from the constants as they stood at 8a3ee4d, before
# services/cost_estimates.py existed. Independent of BASELINE by construction:
# if someone edits a baseline, this file is what disagrees.
LEGACY_CONSTANTS = {
    "summarizer.batch": 0.0027,  # SUMMARY_COST_PER_BATCH_USD
    "context_generator.batch": 0.0013,  # CONTEXT_COST_PER_CALL_USD
    "primer_generator.batch": 0.0016,  # PRIMER_COST_PER_CALL_USD
    "entity_extractor.batch": 0.0020,  # ENTITY_COST_PER_CALL_USD
    "entity_linker_llm.link_text": 0.0042,  # LINK_COST_PER_CALL_FULL_USD
    "entity_linker_llm.link_text.narrowed": 0.00086,  # LINK_COST_PER_CALL_NARROWED_USD
    "story_confirmer.confirm": 0.0063,  # CONFIRM_COST_PER_CALL_USD
    "story_synthesizer.synthesize": 0.0022,  # SYNTHESIS_COST_PER_CALL_USD
    "judge.batch": 0.003,  # JUDGE_COST_PER_LINE_USD
    # COMPARE_COST_ESTIMATE_PER_SOURCE_USD in app/routers/compare.py, and its
    # hand-copy COST_ESTIMATE_PER_SOURCE_USD in services/daily_compare.py.
    "compare.search_sources": 0.04,
}


@pytest.fixture(autouse=True)
def _no_overrides():
    """Every legacy figure was measured on the incumbent."""
    model_registry._parse_overrides.cache_clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(model_registry.settings, "llm_model_overrides", "")
        yield
    model_registry._parse_overrides.cache_clear()


class TestNothingMoved:
    @pytest.mark.parametrize(("baseline_id", "legacy"), sorted(LEGACY_CONSTANTS.items()))
    def test_one_unit_equals_the_legacy_constant(self, baseline_id, legacy):
        assert round(estimate_cost(baseline_id, 1), 6) == round(legacy, 6)

    @pytest.mark.parametrize(("baseline_id", "legacy"), sorted(LEGACY_CONSTANTS.items()))
    def test_n_units_scales_exactly_as_the_old_multiplication_did(
        self, baseline_id, legacy
    ):
        """Every call site was `CONSTANT * n`."""
        for n in (0, 1, 7, 40, 2969):
            assert round(estimate_cost(baseline_id, n), 9) == round(legacy * n, 9)

    def test_every_legacy_constant_has_a_baseline_and_vice_versa(self):
        assert set(BASELINE) == set(LEGACY_CONSTANTS)


class TestTheConstantsAreActuallyGone:
    """A centralization that leaves the old constants behind has centralized
    nothing — it has created a second source of truth that can drift."""

    def test_no_module_still_defines_a_per_call_cost_constant(self):
        offenders = []
        for directory in ("services", "workflows", "app", "scripts"):
            for path in (REPO_ROOT / directory).rglob("*.py"):
                if path.name == "cost_estimates.py":
                    continue
                tree = ast.parse(path.read_text())
                for node in tree.body:
                    if not isinstance(node, ast.Assign):
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.endswith("_USD"):
                            offenders.append(f"{path.name}:{target.id}")
        assert not offenders, (
            f"cost constants still defined outside cost_estimates.py: {offenders}"
        )


class TestUnknownBaseline:
    def test_a_missing_baseline_is_unaffordable_not_free(self, caplog):
        """Zero would authorize an unbounded run. This sits in front of a
        fail-closed guard, so the failure has to be in the blocking direction —
        and it has to say why."""
        with caplog.at_level("ERROR", logger="sift-api.cost_estimates"):
            cost = estimate_cost("stage.nobody.measured", 1)
        assert cost == float("inf")
        assert caplog.records

    def test_it_does_not_raise(self):
        """no-assert-ok: the behaviour under test IS "does not raise".

        estimate_cost sits in front of a fail-closed guard on the ingest path,
        so a baseline someone forgot to add must degrade to a blocked call, not
        to a stack trace that takes the pipeline down. The returned value is
        asserted in the test above; this one only pins that it returns at all.
        """
        estimate_cost("stage.nobody.measured", 3)


class TestOverriddenModelIsBoundedAbove:
    """No stored in:out split exists yet (migrations/029 only just landed), so
    a candidate's cost cannot honestly be projected — only bounded."""

    def test_a_pricier_model_raises_the_estimate(self, caplog):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                "summarizer.batch=sonnet-4-6",
            )
            model_registry._parse_overrides.cache_clear()
            cost_estimates._warned.clear()
            with caplog.at_level("WARNING", logger="sift-api.cost_estimates"):
                cost = estimate_cost("summarizer.batch", 1)

        # Sonnet is 3x Haiku on both input and output, so the bound is 3x.
        assert round(cost, 6) == round(0.0027 * 3, 6)
        assert caplog.records, "an unmeasured model must announce that it is a bound"
        assert "upper bound" in caplog.records[0].getMessage()

    def test_the_bound_takes_the_worse_of_the_two_price_ratios(self):
        """A model cheaper on input and dearer on output must not net out to
        'about the same' — without a token split, the dear side is the only
        safe assumption."""
        lopsided = model_registry.ModelSpec(
            catalog_id="lopsided",
            provider="openai_compatible",
            model="lopsided-model",
            supports_batch=True,
            supports_prompt_cache=False,
            supports_server_web_search=False,
        )
        from services import usage_tracker

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(model_registry.MODELS, "lopsided", lopsided)
            mp.setitem(
                usage_tracker.PRICES,
                "lopsided-model",
                usage_tracker.ModelPrices(0.1, 10.0, 0.125, 0.01),  # 0.1x in, 2x out
            )
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                "summarizer.batch=lopsided",
            )
            model_registry._parse_overrides.cache_clear()
            cost_estimates._warned.clear()
            cost = estimate_cost("summarizer.batch", 1)

        assert round(cost, 6) == round(0.0027 * 2.0, 6)  # the output ratio, not 0.1

    def test_judge_is_bounded_against_sonnet_not_haiku(self):
        """Its baseline was measured on Sonnet, so the ratio is relative to
        Sonnet. Comparing against Haiku would report moving judge->Haiku as a
        3x *increase*."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                model_registry.settings, "llm_model_overrides", "judge.batch=haiku-4-5"
            )
            model_registry._parse_overrides.cache_clear()
            cost_estimates._warned.clear()
            cost = estimate_cost("judge.batch", 1)

        # Haiku is 1/3 of Sonnet on both axes, so the bound is 1/3.
        assert round(cost, 6) == round(0.003 / 3, 6)


class TestBatchDiscountIsAModelProperty:
    def test_anthropic_models_carry_the_fifty_percent_discount(self):
        assert model_registry.batch_price_multiplier_for("claude-haiku-4-5-20251001") == 0.5
        assert model_registry.batch_price_multiplier_for("claude-sonnet-4-6") == 0.5

    def test_the_undated_alias_resolves_too(self):
        """Both forms are in ai_usage_daily, and log_usage's own default is the
        alias. A lookup that missed it would silently drop the discount and
        double the recorded cost of three stages."""
        assert model_registry.batch_price_multiplier_for("claude-haiku-4-5") == 0.5

    def test_an_unknown_model_gets_no_discount(self):
        """Overstating is the safe direction against a fail-closed ceiling:
        an over-reported figure trips the guard early, an under-reported one
        lets a run past it."""
        assert model_registry.batch_price_multiplier_for("some-open-weight-model") == 1.0

    def test_the_anthropic_multiplier_still_matches_the_documented_rate(self):
        from services import usage_tracker

        assert model_registry.batch_price_multiplier_for(
            "claude-haiku-4-5-20251001"
        ) == usage_tracker.BATCH_DISCOUNT


class TestProvenanceIsRecorded:
    def test_every_baseline_says_where_its_number_came_from(self):
        """These are measured averages, not derivations. Without provenance the
        next person cannot tell a measurement from a guess — and the repo's own
        rule is to re-baseline from scripts/verify_cost_baseline.py, never by
        hand."""
        for baseline_id, base in BASELINE.items():
            assert len(base.provenance) > 40, baseline_id

    def test_every_baseline_names_a_real_operation(self):
        for baseline_id, base in BASELINE.items():
            assert base.operation in model_registry.OPERATIONS, baseline_id

    def test_the_unit_is_stated_because_it_is_not_always_a_call(self):
        """judge.batch is per *line* and compare is per *source*; the rest are
        per call. A caller passing the wrong unit is off by the batch size."""
        assert BASELINE["judge.batch"].unit == "line"
        assert BASELINE["compare.search_sources"].unit == "source"
        assert BASELINE["summarizer.batch"].unit == "call"
