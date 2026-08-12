"""What a paid call is expected to cost, for the pre-call budget check.

WHY THIS EXISTS
---------------
Eleven measured dollar figures were scattered across eight files, two of them
literal copies of each other under a comment saying so. Every one is a
**measured average from `ai_usage_daily`**, not a token-count derivation — the
guard only needs to be right enough to stop a run near the ceiling, and an
estimate that tracks reality beats one that is defensibly derived and 5x off.

That was fine while every stage ran Haiku forever. It stops being fine the
moment `services/model_registry.py` can put a stage on a different model,
because all eleven were measured on the incumbent and a swap invalidates every
one of them — asymmetrically:

  too low   -> the guard authorizes a run that blows the ceiling. It is the
               only backstop, and it fails in the expensive direction.
  too high  -> the guard blocks a run that would have fit, and the summarizer
               degrades to truncated RSS text that nothing revisits. A quality
               regression presenting as a budget event.

WHY THIS DOES NOT PROJECT FROM TOKEN COUNTS
-------------------------------------------
The obvious move is to store an input/output token shape per operation and
re-price it against any model. It is also the move that would produce
confident, wrong numbers today: `ai_usage_daily` recorded dollars only until
migrations/029, so there is no stored in:out split to derive a shape from. Any
shape written here now would be reverse-engineered to reproduce the dollar
figure it is supposed to explain — one equation, two unknowns, and infinitely
many splits that fit. The projection would look rigorous and be arbitrary.

So instead, for a model this repo has never measured, `estimate_cost` returns a
deliberate **upper bound**: the incumbent's measured cost scaled by the worse of
the candidate's input and output price ratios. Overstating is the safe
direction against a fail-closed ceiling, and it is honest about being a bound
rather than a prediction.

**Replace this once 029 has collected data.** A few days of real token counts
per (operation, model) turns the bound into an actual projection, and that is
the thing that lets a candidate be shortlisted on arithmetic instead of on
spend. Until then, a bound that says so beats a number that doesn't.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from services import usage_tracker
from services.model_registry import INCUMBENT_BY_OPERATION, MODELS, resolve

logger = logging.getLogger("sift-api.cost_estimates")


@dataclass(frozen=True)
class OperationBaseline:
    """One measured cost, and enough provenance to re-derive it."""

    operation: str  # the model_registry operation this is measured on
    usd_per_unit: float
    unit: str  # what `units` counts — "call", "line", "source"
    provenance: str


# Every figure below is measured, and the provenance says from what. Do not
# adjust one by reasoning about token counts; re-run
# `scripts/verify_cost_baseline.py` and take the number it prints.
BASELINE: dict[str, OperationBaseline] = {
    "summarizer.batch": OperationBaseline(
        operation="summarizer.batch",
        usd_per_unit=0.0027,
        unit="call",
        provenance=(
            "ai_usage_daily, 7 days to 2026-08-11: $7.91 across 2,969 calls. "
            "docs/SOURCE_SCALING.md"
        ),
    ),
    "context_generator.batch": OperationBaseline(
        operation="context_generator.batch",
        usd_per_unit=0.0013,
        unit="call",
        provenance=(
            "ai_usage_daily, 7 days to 2026-08-11: $1.55 across 1,231 calls, "
            "already net of the 50% Batch API discount. docs/SOURCE_SCALING.md"
        ),
    ),
    "primer_generator.batch": OperationBaseline(
        operation="primer_generator.batch",
        usd_per_unit=0.0016,
        unit="call",
        provenance=(
            "ai_usage_daily, 7 days to 2026-08-11: $3.52 across 2,308 calls, "
            "already net of the 50% Batch API discount. docs/SOURCE_SCALING.md"
        ),
    ),
    "entity_extractor.batch": OperationBaseline(
        operation="entity_extractor.batch",
        usd_per_unit=0.0020,
        unit="call",
        provenance=(
            "ai_usage_daily, 7 days to 2026-08-11: $1.74 across 872 calls, "
            "already net of the 50% Batch API discount. docs/SOURCE_SCALING.md"
        ),
    ),
    # Two baselines for one operation: which applies depends on
    # `entity_linker_roster_narrowing_enabled`, and they differ by 5x. Using the
    # full-roster figure while narrowing is live would trip the ceiling at a
    # fifth of the real spend.
    "entity_linker_llm.link_text": OperationBaseline(
        operation="entity_linker_llm.link_text",
        usd_per_unit=0.0042,
        unit="call",
        provenance=(
            "ai_usage_daily, 7 days to 2026-08-11: $22.38 across 5,409 calls, "
            "full roster. docs/SOURCE_SCALING.md"
        ),
    ),
    "entity_linker_llm.link_text.narrowed": OperationBaseline(
        operation="entity_linker_llm.link_text",
        usd_per_unit=0.00086,
        unit="call",
        provenance=(
            "$0.000857/call observed end to end once roster narrowing landed — "
            "the roster went from ~7,000 tokens to ~600. docs/SOURCE_SCALING.md"
        ),
    ),
    "story_confirmer.confirm": OperationBaseline(
        operation="story_confirmer.confirm",
        usd_per_unit=0.0063,
        unit="call",
        provenance="ai_usage_daily, 7 days to 2026-08-11: $1.45 across 230 calls",
    ),
    "story_synthesizer.synthesize": OperationBaseline(
        operation="story_synthesizer.synthesize",
        usd_per_unit=0.0022,
        unit="call",
        provenance=(
            "ai_usage_daily, 7 days to 2026-08-11: $10.14 across 4,564 calls. "
            "An upper bound per relevant candidate — most attaches skip "
            "synthesis entirely because the outlet set did not change — so the "
            "guard trips slightly early, which is right for a ceiling."
        ),
    ),
    "judge.batch": OperationBaseline(
        operation="judge.batch",
        usd_per_unit=0.003,
        unit="line",
        provenance=(
            "Rough Sonnet cost per line (title+summary+line in, short out). "
            "Pre-checks the guard before the optional runtime judge."
        ),
    ),
    # One entry, two call sites. app/routers/compare.py and
    # services/daily_compare.py each carried this as a literal 0.04, the second
    # under a comment saying it mirrored the first — which is a copy that can
    # drift, not a shared constant.
    "compare.search_sources": OperationBaseline(
        operation="compare.search_sources",
        usd_per_unit=0.04,
        unit="source",
        provenance=(
            "Conservative: ~one web search per source (~$0.01) plus Claude "
            "tokens for search + extraction. Deliberately high so a compare is "
            "blocked *before* it would cross the ceiling, not after. "
            "sift-api#70"
        ),
    ),
}


def estimate_cost(baseline_id: str, units: float = 1) -> float:
    """Expected USD for `units` of `baseline_id`, at whichever model is resolved.

    On the incumbent this returns the measured figure unchanged, so the guard
    behaves exactly as it did before the registry existed. On an overridden
    model it returns an upper bound — see the module docstring for why a bound
    and not a projection.
    """
    base = BASELINE.get(baseline_id)
    if base is None:
        # Never raise: this sits in front of a fail-closed guard, and a missing
        # baseline must not become an outage. Zero is the wrong fallback (it
        # would authorize an unbounded run), so refuse by returning a cost no
        # budget can absorb, and say why.
        logger.error(
            "cost_estimates: no baseline for %r — treating it as unaffordable. "
            "Add it to BASELINE.",
            baseline_id,
        )
        return float("inf")

    return base.usd_per_unit * units * _model_multiplier(base.operation)


def _model_multiplier(operation: str) -> float:
    """1.0 on the incumbent; a conservative upper bound on anything else."""
    spec = resolve(operation)
    incumbent = MODELS[INCUMBENT_BY_OPERATION[operation]]
    if spec.model == incumbent.model:
        return 1.0

    measured = usage_tracker.prices_for(incumbent.model)
    candidate = usage_tracker.prices_for(spec.model)

    # The worse of the two ratios. Without a stored in:out split there is no
    # honest way to blend them, and picking the larger keeps the estimate above
    # the truth rather than below it — the direction a fail-closed ceiling can
    # survive.
    multiplier = max(
        candidate.input_per_m / measured.input_per_m,
        candidate.output_per_m / measured.output_per_m,
    )

    key = (operation, spec.model)
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "cost_estimates: %s is on %s, which has no measured baseline. "
            "Estimating at %.2fx the %s figure — an upper bound, not a "
            "projection. Re-derive from ai_usage_daily token columns "
            "(migrations/029) once this model has run for a few days.",
            operation,
            spec.model,
            multiplier,
            incumbent.model,
        )
    return multiplier


_warned: set[tuple[str, str]] = set()
