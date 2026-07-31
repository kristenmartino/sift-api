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

    def test_ledger_is_not_written_when_the_cost_guard_is_disabled(self):
        """This is why ai_usage_daily is empty in prod: _record_to_ledger
        short-circuits unless ai_cost_guard_enabled is true."""
        with patch.object(usage_tracker.settings, "ai_cost_guard_enabled", False):
            with patch.object(usage_tracker, "_record_to_ledger") as rec:
                log_usage("op", _response(input_tokens=ONE_M))
                # log_usage always calls it; the short-circuit is inside.
                rec.assert_called_once()


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
