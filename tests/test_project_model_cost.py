"""The projection's arithmetic, and the discount it must not over-apply.

This script decides which candidate models are worth paying to evaluate. Its
failure mode is quiet: a projection that is wrong in the cheap direction ships
a model that costs more than it promised, and nothing catches it until a bill
arrives a month later.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent

# scripts/ is not a package, so load by path. Registering in sys.modules before
# exec is required: @dataclass resolves its own module out of sys.modules, and
# without this the decorator raises on an unregistered module.
_spec = importlib.util.spec_from_file_location(
    "project_model_cost", REPO_ROOT / "scripts" / "project_model_cost.py"
)
pmc = importlib.util.module_from_spec(_spec)
sys.modules["project_model_cost"] = pmc
_spec.loader.exec_module(pmc)


def _row(**kw):
    base = {
        "operation": "summarizer.batch",
        "model": "claude-haiku-4-5-20251001",
        "calls": 1,
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "usd": 0.0,
    }
    base.update(kw)
    return base


HAIKU = pmc.Candidate("haiku", 1.0, 5.0, has_batch_discount=True)
NO_BATCH = pmc.Candidate("open-weight", 1.0, 5.0, has_batch_discount=False)


class TestBatchDiscountAppliesToStagesNotModels:
    """The bug this test exists for.

    `uses_batch` was originally read off the MODEL (Haiku has a batch API) and
    applied to every row, so realtime stages — summarizer, the linker, the
    synthesizer, the confirmer — were projected at half price. The incumbent
    total came out at $1.364/1k against a measured $2.17, which makes every
    candidate look better than it is: an error in the direction that gets a
    model shipped.
    """

    def test_a_realtime_stage_gets_no_discount(self):
        cost = pmc.project(_row(), HAIKU, batch_multiplier=0.5, uses_batch=False)
        assert cost == pytest.approx(1.00)  # 1M input x $1/M, undiscounted

    def test_a_batch_stage_gets_the_discount(self):
        cost = pmc.project(_row(), HAIKU, batch_multiplier=0.5, uses_batch=True)
        assert cost == pytest.approx(0.50)

    def test_a_candidate_without_a_batch_api_pays_full_price_on_a_batch_stage(self):
        """Three stages depend on Anthropic's 50%. A provider without one does
        not merely cost more per token — the real comparison on those stages is
        2x what a token projection alone suggests."""
        cost = pmc.project(_row(), NO_BATCH, batch_multiplier=0.5, uses_batch=True)
        assert cost == pytest.approx(1.00)


class TestTokenArithmetic:
    def test_input_and_output_are_priced_separately(self):
        cost = pmc.project(
            _row(input_tokens=1_000_000, output_tokens=1_000_000),
            HAIKU,
            batch_multiplier=1.0,
            uses_batch=False,
        )
        assert cost == pytest.approx(6.00)  # $1 in + $5 out

    def test_cache_tokens_are_priced_as_plain_input(self):
        """Prompt-caching semantics differ per provider and most
        OpenAI-compatible hosts report nothing, so assuming the discount
        carries over would understate a candidate. Overstating is the safe
        direction."""
        cost = pmc.project(
            _row(input_tokens=0, cache_read=1_000_000, cache_write=0),
            HAIKU,
            batch_multiplier=1.0,
            uses_batch=False,
        )
        assert cost == pytest.approx(1.00)  # full input rate, not $0.10/M

    def test_output_price_dominates_at_sifts_measured_ratio(self):
        """The finding this script produced: pipeline-wide out:in is ~0.25, and
        output is priced 5x, so output is ~56% of the bill despite being ~20%
        of the tokens. A candidate has to be cheap on OUTPUT to matter — being
        10x cheaper on input alone moves the total very little.
        """
        row = _row(input_tokens=1_000_000, output_tokens=250_000)
        base = pmc.project(row, HAIKU, 1.0, uses_batch=False)
        assert base == pytest.approx(2.25)  # $1.00 input + $1.25 output

        # Output is 20% of the tokens and 1.25/2.25 = 56% of the cost.
        output_share = (250_000 * 5.0 / 1e6) / base
        assert output_share == pytest.approx(0.5556, abs=1e-4)

        cheap_input = pmc.Candidate("cheap-in", 0.10, 5.0, False)
        cheap_output = pmc.Candidate("cheap-out", 1.0, 0.50, False)

        in_saving = 1 - pmc.project(row, cheap_input, 1.0, uses_batch=False) / base
        out_saving = 1 - pmc.project(row, cheap_output, 1.0, uses_batch=False) / base

        # The same 10x discount is worth more on output than on input here.
        assert in_saving == pytest.approx(0.40)
        assert out_saving == pytest.approx(0.50)
        assert in_saving < out_saving


class TestZeroTokenRowsAreNotSilentlyPriced:
    def test_a_row_with_no_tokens_projects_to_zero(self):
        """Rows written before migrations/029 carry dollars but no tokens.
        They must contribute nothing to a projection rather than being
        back-filled with a guess — and the report flags the all-zero case
        loudly, because arithmetic on nothing is the failure this script
        would otherwise hide."""
        cost = pmc.project(
            _row(input_tokens=0, output_tokens=0), HAIKU, 1.0, uses_batch=False
        )
        assert cost == 0.0

    def test_an_all_zero_window_refuses_to_report(self, capsys):
        report = pmc.main_report([_row(input_tokens=0, output_tokens=0)], 1000.0, 1)
        assert report == {"tokens_present": False}
        assert "TOKENS ARE ALL ZERO" in capsys.readouterr().out
