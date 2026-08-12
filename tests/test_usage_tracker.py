"""Tests for services.usage_tracker.

This module had no tests, despite being the only thing that knows what the
pipeline costs — and despite `log_usage` wrapping its entire body in a bare
`except Exception: return {}`, which turns any internal bug into a silent
zero-telemetry no-op.

The price table here is duplicated in TypeScript at sift/lib/usage-tracker.ts.
See TestPriceTableParity for the cross-language contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from services import usage_tracker
from services.usage_tracker import count_web_searches, log_usage

ONE_M = 1_000_000


def _response(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    content: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
        content=content or [],
    )


class TestPriceTableParity:
    """The cross-language contract.

    services/usage_tracker.py and sift/lib/usage-tracker.ts each hardcode the
    same five Anthropic prices, in two languages, in two independent git repos,
    with no shared source. The identical assertion lives in
    sift/__tests__/usage-tracker.test.ts — if either table drifts, exactly one
    of the two suites goes red.

    One synthetic payload exercises all five constants at once:
        1M input        x $1.00/M  = 1.00
        1M output       x $5.00/M  = 5.00
        1M cache write  x $1.25/M  = 1.25
        1M cache read   x $0.10/M  = 0.10
        3 web searches  x $0.010   = 0.03
                                   ------
                                     7.38
    """

    GOLDEN_COST_USD = 7.38

    def test_golden_cost(self):
        payload = log_usage(
            "test",
            _response(
                input_tokens=ONE_M,
                output_tokens=ONE_M,
                cache_creation=ONE_M,
                cache_read=ONE_M,
            ),
            web_searches=3,
        )
        assert payload["cost_usd"] == self.GOLDEN_COST_USD

    def test_each_constant_individually(self):
        """So a failure names which price drifted, not just that one did."""
        cases = [
            ("input", {"input_tokens": ONE_M}, 1.00),
            ("output", {"output_tokens": ONE_M}, 5.00),
            ("cache_write", {"cache_creation": ONE_M}, 1.25),
            ("cache_read", {"cache_read": ONE_M}, 0.10),
        ]
        for name, kwargs, expected in cases:
            payload = log_usage("test", _response(**kwargs))
            assert payload["cost_usd"] == expected, f"{name} price drifted"

        payload = log_usage("test", _response(), web_searches=1)
        assert payload["cost_usd"] == 0.010, "web search price drifted"

    def test_the_model_table_agrees_with_the_pinned_constants(self):
        """`PRICES` is keyed by model; the five constants above are what two
        sibling repos are pinned to. If the default model's row and those
        constants ever disagree, the golden 7.38 goes on asserting a number this
        module no longer charges — green here, wrong in the ledger."""
        haiku = usage_tracker.PRICES[usage_tracker.DEFAULT_MODEL]
        assert haiku.input_per_m == usage_tracker.PRICE_INPUT_PER_M
        assert haiku.output_per_m == usage_tracker.PRICE_OUTPUT_PER_M
        assert haiku.cache_write_per_m == usage_tracker.PRICE_CACHE_WRITE_5M_PER_M
        assert haiku.cache_read_per_m == usage_tracker.PRICE_CACHE_READ_PER_M


class TestModelAwarePricing:
    """`log_usage` took a `model` argument and ignored it for pricing.

    Everything on the pipeline is Haiku, so that read as harmless — but
    services/judge.py runs Sonnet, and every judge call was costed at Haiku's
    $1/$5 instead of $3/$15: understated ~3x. Nothing in prod spends it today
    (`why_it_matters_judge_enabled` defaults false), so the damage was confined
    to eval scripts under-reporting their own cost — and to the ledger being
    ready to record fiction the moment a second model ran anywhere.
    """

    def test_sonnet_is_not_billed_at_haiku_rates(self):
        haiku = log_usage("op", _response(input_tokens=ONE_M, output_tokens=ONE_M))
        sonnet = log_usage(
            "op",
            _response(input_tokens=ONE_M, output_tokens=ONE_M),
            model="claude-sonnet-4-6",
        )
        assert haiku["cost_usd"] == 6.00  # $1 in + $5 out
        assert sonnet["cost_usd"] == 18.00  # $3 in + $15 out
        assert sonnet["cost_usd"] == haiku["cost_usd"] * 3

    def test_the_dated_haiku_snapshot_prices_the_same_as_the_alias(self):
        """services/ pins claude-haiku-4-5-20251001 while log_usage's default is
        the alias. Both must price identically or the same call costs two
        different amounts depending on which string reached the ledger."""
        alias = log_usage("op", _response(input_tokens=ONE_M), model="claude-haiku-4-5")
        dated = log_usage(
            "op", _response(input_tokens=ONE_M), model="claude-haiku-4-5-20251001"
        )
        assert alias["cost_usd"] == dated["cost_usd"] == 1.00

    def test_an_unpriced_model_falls_back_loudly_rather_than_to_zero(self):
        """A zero looks like "this stage is free" and survives review; a wrong
        number is visible and gets corrected. So the fallback bills at the
        default model's rates and says so."""
        usage_tracker._warned_models.discard("some-open-weight-model")
        with patch.object(usage_tracker.logger, "warning") as warn:
            payload = log_usage(
                "op", _response(input_tokens=ONE_M), model="some-open-weight-model"
            )
        assert payload["cost_usd"] == 1.00  # Haiku's input rate, not $0
        warn.assert_called_once()

    def test_the_unpriced_warning_fires_once_per_model_not_once_per_call(self):
        usage_tracker._warned_models.discard("noisy-model")
        with patch.object(usage_tracker.logger, "warning") as warn:
            for _ in range(5):
                log_usage("op", _response(input_tokens=1), model="noisy-model")
        warn.assert_called_once()

    def test_batch_usage_is_model_aware_too(self):
        """The three Batch API stages price through a different function; a fix
        applied to only one of them is the same bug in half the pipeline."""
        results = [_batch_result(input_tokens=ONE_M)]
        haiku = usage_tracker.log_batch_usage("op", results)
        sonnet = usage_tracker.log_batch_usage("op", results, model="claude-sonnet-4-6")
        assert haiku["cost_usd"] == 0.50  # $1/M x 1M x 50% batch discount
        assert sonnet["cost_usd"] == 1.50


class TestLogUsage:
    def test_returns_a_payload_rather_than_a_swallowed_empty_dict(self):
        """`log_usage` catches every exception and returns {}. Without this
        test, a bug anywhere in its body would silently disable all cost
        telemetry and no suite would notice."""
        payload = log_usage("summarizer.batch", _response(input_tokens=100, output_tokens=50))
        assert payload != {}
        assert payload["event"] == "api_usage"
        assert payload["operation"] == "summarizer.batch"

    def test_reports_every_token_field(self):
        payload = log_usage(
            "op",
            _response(input_tokens=1, output_tokens=2, cache_creation=3, cache_read=4),
        )
        assert payload["input_tokens"] == 1
        assert payload["output_tokens"] == 2
        assert payload["cache_creation_input_tokens"] == 3
        assert payload["cache_read_input_tokens"] == 4

    def test_missing_usage_is_treated_as_zero_not_an_error(self):
        payload = log_usage("op", SimpleNamespace(usage=None, content=[]))
        assert payload["cost_usd"] == 0
        assert payload["input_tokens"] == 0

    def test_malformed_response_reports_zero_cost_rather_than_raising(self):
        """Telemetry never breaks the pipeline — but note the consequence:
        an object with no `usage` attribute is not an error, it is billed as
        $0. A malformed response therefore silently UNDER-reports spend rather
        than surfacing a problem. Pinned deliberately so a future change to
        this behavior is a conscious one."""
        for bad in (object(), None):
            payload = log_usage("op", bad)
            assert payload["cost_usd"] == 0.0
            assert payload["input_tokens"] == 0

    def test_model_is_recorded_for_breakdown(self):
        payload = log_usage("op", _response(), model="claude-sonnet-4-6")
        assert payload["model"] == "claude-sonnet-4-6"

    def test_ledger_is_written_with_the_computed_cost(self):
        with patch.object(usage_tracker, "_record_to_ledger") as rec:
            log_usage("op", _response(input_tokens=ONE_M))
        rec.assert_called_once()
        assert rec.call_args.args[2] == 1.0  # $1.00/M input

    def test_token_counts_reach_the_ledger_not_just_the_log_line(self):
        """These four numbers were computed and thrown away — the log line had
        them, the ledger did not, so the stored dollars could not be re-priced
        onto another model (migrations/029)."""
        with patch.object(usage_tracker, "_record_to_ledger") as rec:
            log_usage(
                "op",
                _response(
                    input_tokens=11, output_tokens=22, cache_read=33, cache_creation=44
                ),
                web_searches=5,
            )
        kw = rec.call_args.kwargs
        assert kw["input_tokens"] == 11
        assert kw["output_tokens"] == 22
        assert kw["cache_read_tokens"] == 33
        assert kw["cache_write_tokens"] == 44
        assert kw["web_search_calls"] == 5

    def test_recording_cannot_be_gated_on_a_setting(self):
        """The structural guard for the 20x-stale cost figure.

        The predecessor of this test asserted the bug and said so in its own
        docstring: "This is why ai_usage_daily is empty in prod."
        `_record_to_ledger` short-circuited unless `ai_cost_guard_enabled`, so
        the flag that turns on *blocking* also turned on *measuring* — and with
        the default `false` the ledger was never written from its creation
        until 2026-07-30, while STATUS.md quoted ~$15/mo against ~$300/mo real.

        This module now imports no settings at all, so recording cannot be made
        conditional on one without that becoming visible here. Enforcement
        lives in cost_guard.check_budget and is still flag-gated, correctly.
        """
        assert not hasattr(usage_tracker, "settings"), (
            "usage_tracker imported settings again — recording must not depend "
            "on configuration. Enforcement belongs in cost_guard.check_budget."
        )


class TestCountWebSearches:
    def test_counts_web_search_blocks(self):
        content = [
            SimpleNamespace(type="server_tool_use", name="web_search"),
            SimpleNamespace(type="text", text="hi"),
            SimpleNamespace(type="server_tool_use", name="web_search"),
        ]
        assert count_web_searches(_response(content=content)) == 2

    def test_ignores_other_server_tools(self):
        content = [SimpleNamespace(type="server_tool_use", name="code_execution")]
        assert count_web_searches(_response(content=content)) == 0

    def test_returns_zero_on_garbage_rather_than_raising(self):
        assert count_web_searches(object()) == 0
        assert count_web_searches(None) == 0


# ── log_batch_usage ─────────────────────────────────────────
#
# The three Message Batches paths recorded nothing at all until 2026-08-05, so
# their spend was invisible: the ledger totalled ~$8.99/day against a real bill
# of ~$10/day, and the gap was them. These pin the two things that make batch
# accounting different from the live path — the result shape and the price.


def _batch_result(*, input_tokens=0, output_tokens=0, cache_read=0,
                  cache_creation=0, succeeded=True):
    """A parsed JSONL row as batch_client hands it back — dicts, not objects."""
    if not succeeded:
        return {"custom_id": "x", "result": {"type": "errored"}}
    return {
        "custom_id": "x",
        "result": {
            "type": "succeeded",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
            },
        },
    }


class TestLogBatchUsage:
    def test_applies_the_fifty_percent_batch_discount(self):
        """Charging batch tokens at list price would overstate this spend 2x."""
        with patch.object(usage_tracker, "_record_to_ledger"):
            out = usage_tracker.log_batch_usage(
                "context_generator.batch",
                [_batch_result(input_tokens=ONE_M, output_tokens=ONE_M)],
            )
        # (1.00 + 5.00) at list, halved.
        assert out["cost_usd"] == 3.0
        assert out["batch"] is True

    def test_reads_the_dict_shape_not_the_object_shape(self):
        """log_usage's getattr access reads nothing off these dicts and would
        silently record $0 — which is worse than not recording, because it
        looks like data."""
        with patch.object(usage_tracker, "_record_to_ledger"):
            out = usage_tracker.log_batch_usage(
                "primer_generator.batch",
                [_batch_result(input_tokens=1000, output_tokens=500)],
            )
        assert out["input_tokens"] == 1000
        assert out["output_tokens"] == 500
        assert out["cost_usd"] > 0

    def test_aggregates_and_counts_errored_separately(self):
        with patch.object(usage_tracker, "_record_to_ledger") as rec:
            out = usage_tracker.log_batch_usage(
                "entity_extractor.batch",
                [
                    _batch_result(input_tokens=100, output_tokens=10),
                    _batch_result(input_tokens=200, output_tokens=20),
                    _batch_result(succeeded=False),
                ],
            )
        assert out["input_tokens"] == 300
        assert out["requests_succeeded"] == 2
        assert out["requests_errored"] == 1
        # call_count is the succeeded count, not len(results).
        assert rec.call_args.kwargs["call_count"] == 2

    def test_empty_and_malformed_results_do_not_raise(self):
        with patch.object(usage_tracker, "_record_to_ledger"):
            assert usage_tracker.log_batch_usage("op", [])["cost_usd"] == 0
            assert usage_tracker.log_batch_usage("op", [{}])["requests_errored"] == 1
            assert usage_tracker.log_batch_usage(
                "op", [{"result": {"type": "succeeded"}}]
            )["cost_usd"] == 0
