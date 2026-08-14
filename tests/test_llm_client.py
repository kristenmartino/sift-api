"""The adapter's normalization, which is where a provider swap goes wrong quietly.

None of these call a network. The failures they guard are all of the same kind:
a field that reads as 0 or "unknown" on the other provider, producing telemetry
that looks like data and is not.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import llm_client, model_registry, usage_tracker
from services.llm_client import LLMResponse, LLMUsage, _normalize_stop_reason, complete

HAIKU = model_registry.MODELS["haiku-4-5"]
NANO = model_registry.MODELS["gpt-5-nano"]


def _anthropic_response(*, text="[]", stop="end_turn", tin=100, tout=50,
                        cache_read=0, cache_write=0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model="claude-haiku-4-5-20251001",
        usage=SimpleNamespace(
            input_tokens=tin, output_tokens=tout,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


def _openai_response(*, text="[]", finish="stop", prompt=100, completion=50, cached=0):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text), finish_reason=finish
        )],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


def _fake(kind, response):
    c = AsyncMock()
    if kind == "anthropic":
        c.messages.create = AsyncMock(return_value=response)
    else:
        c.chat.completions.create = AsyncMock(return_value=response)
    return c


class TestStopReasonNormalization:
    """migrations/021 and story_clusterer's truncation warning both key on the
    Anthropic strings. Unnormalized, llm_output_stops would silently stop
    recording truncation the moment a stage changed provider — and truncation
    is the thing it exists to detect, because a response cut at the cap is
    truncated JSON and fails alignment exactly like a scrambled one."""

    @pytest.mark.parametrize(("raw", "expected"), [
        ("length", "max_tokens"),      # the one that matters
        ("stop", "end_turn"),
        ("content_filter", "refusal"),
        ("tool_calls", "tool_use"),
        ("max_tokens", "max_tokens"),  # Anthropic passes through
        ("end_turn", "end_turn"),
        (None, "unknown"),
    ])
    def test_maps_onto_the_repo_vocabulary(self, raw, expected):
        assert _normalize_stop_reason(raw) == expected

    def test_an_unknown_reason_passes_through_rather_than_being_swallowed(self):
        assert _normalize_stop_reason("some_new_reason") == "some_new_reason"


class TestUsageMeansTheSameThingOnBothProviders:
    @pytest.mark.asyncio
    async def test_openai_prompt_tokens_are_split_from_cached(self):
        """OpenAI reports prompt_tokens as the TOTAL including cached; Anthropic
        reports them separately. Left as-is, `input_tokens + cache_read_tokens`
        would double-count the cached portion on one provider and not the
        other, and the ledger's token columns are what project_model_cost.py
        re-prices."""
        client = _fake("openai", _openai_response(prompt=1000, cached=400))
        r = await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                           spec=NANO, client=client)
        assert r.usage.input_tokens == 600
        assert r.usage.cache_read_tokens == 400
        assert r.usage.input_tokens + r.usage.cache_read_tokens == 1000

    @pytest.mark.asyncio
    async def test_anthropic_fields_map_straight_across(self):
        client = _fake("anthropic", _anthropic_response(
            tin=100, tout=50, cache_read=7, cache_write=3))
        r = await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                           spec=HAIKU, client=client)
        assert (r.usage.input_tokens, r.usage.output_tokens) == (100, 50)
        assert (r.usage.cache_read_tokens, r.usage.cache_write_tokens) == (7, 3)

    @pytest.mark.asyncio
    async def test_a_missing_usage_block_reports_zero_rather_than_raising(self):
        client = _fake("openai", SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop")],
            usage=None,
        ))
        r = await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                           spec=NANO, client=client)
        assert r.usage.output_tokens == 0

    @pytest.mark.asyncio
    async def test_cache_write_is_zero_not_guessed_on_openai(self):
        """No OpenAI-compatible provider bills a cache WRITE premium. Reporting
        a guessed number would put fiction in the ledger."""
        client = _fake("openai", _openai_response(prompt=100, cached=50))
        r = await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                           spec=NANO, client=client)
        assert r.usage.cache_write_tokens == 0


class TestTemperatureIsExplicit:
    """Nothing in this repo has ever set temperature, so every call has run at
    whatever the vendor chose. index_alignment states non-zero temperature as a
    premise for its retry being a genuinely different draw — and the number is
    not portable, since Anthropic's range is 0-1 and OpenAI's is 0-2."""

    @pytest.mark.asyncio
    async def test_the_spec_supplies_a_temperature_when_the_caller_does_not(self):
        client = _fake("anthropic", _anthropic_response())
        await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                       spec=HAIKU, client=client)
        assert client.messages.create.call_args.kwargs["temperature"] == (
            HAIKU.default_temperature
        )

    @pytest.mark.asyncio
    async def test_an_explicit_temperature_wins(self):
        client = _fake("openai", _openai_response())
        await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                       temperature=0.0, spec=NANO, client=client)
        assert client.chat.completions.create.call_args.kwargs["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_it_is_never_left_to_the_provider_default(self):
        for spec, kind in ((HAIKU, "anthropic"), (NANO, "openai")):
            resp = _anthropic_response() if kind == "anthropic" else _openai_response()
            client = _fake(kind, resp)
            await complete(operation="summarizer.batch", user="hi", max_tokens=10,
                           spec=spec, client=client)
            call = (client.messages.create if kind == "anthropic"
                    else client.chat.completions.create)
            assert "temperature" in call.call_args.kwargs, spec.catalog_id


class TestRequestShape:
    @pytest.mark.asyncio
    async def test_system_becomes_a_system_message_on_openai(self):
        client = _fake("openai", _openai_response())
        await complete(operation="summarizer.batch", user="u", system="s",
                       max_tokens=10, spec=NANO, client=client)
        msgs = client.chat.completions.create.call_args.kwargs["messages"]
        assert msgs[0] == {"role": "system", "content": "s"}
        assert msgs[1] == {"role": "user", "content": "u"}

    @pytest.mark.asyncio
    async def test_cache_control_is_a_hint_that_no_ops_off_anthropic(self):
        client = _fake("openai", _openai_response())
        await complete(operation="summarizer.batch", user="u", system="s",
                       max_tokens=10, cache_system=True, spec=NANO, client=client)
        # Nothing cache-shaped should have been sent.
        sent = str(client.chat.completions.create.call_args.kwargs)
        assert "cache_control" not in sent

    @pytest.mark.asyncio
    async def test_json_object_mode_is_not_requested(self):
        """It forces a top-level object, and every parser here wants an array.
        Silent breakage for a marginal gain."""
        client = _fake("openai", _openai_response())
        await complete(operation="summarizer.batch", user="u", max_tokens=10,
                       spec=NANO, client=client)
        assert "response_format" not in client.chat.completions.create.call_args.kwargs


class TestFailuresReachTheCaller:
    @pytest.mark.asyncio
    async def test_a_provider_error_raises_rather_than_returning_empty(self):
        """Every caller already has a degradation contract — truncated RSS,
        regex linking, a first-article copy. Swallowing here would take that
        decision away from the code that knows what a safe failure looks
        like."""
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("503"))
        with pytest.raises(llm_client.LLMClientError, match="gpt-5-nano"):
            await complete(operation="summarizer.batch", user="u", max_tokens=10,
                           spec=NANO, client=client)

    def test_a_missing_key_names_the_setting_and_says_not_to_set_it_in_prod(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(llm_client.settings, "openai_api_key", "")
            with pytest.raises(llm_client.LLMClientError) as e:
                llm_client._api_key(NANO)
        msg = str(e.value)
        assert "OPENAI_API_KEY" in msg
        assert "Railway" in msg


class TestLedgerHandoff:
    def test_an_llm_response_is_priced_at_its_own_model_rate(self):
        """log_usage reads its fields off an anthropic Message with getattr.
        Hand it an OpenAI response and all four reads return 0, cost computes
        to 0, and _record_to_ledger returns early — recording nothing while
        looking like data."""
        resp = LLMResponse(
            text="[]",
            usage=LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000),
            stop_reason="end_turn", spec=NANO, latency_ms=1.0,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(usage_tracker, "_record_to_ledger", lambda *a, **k: None)
            payload = llm_client.log_llm_usage("summarizer.batch", resp)

        assert payload["model"] == "gpt-5-nano"
        assert payload["input_tokens"] == 1_000_000
        # $0.05/M in + $0.40/M out — NOT Haiku's $1/$5.
        assert payload["cost_usd"] == pytest.approx(0.45)

    def test_latency_is_recorded(self):
        """A cheaper model behind a slower endpoint is not cheaper: the
        pipeline has to finish inside a 30-minute REFRESH_INTERVAL."""
        resp = LLMResponse(text="", usage=LLMUsage(), stop_reason="end_turn",
                           spec=NANO, latency_ms=1234.5)
        assert resp.latency_ms == 1234.5
