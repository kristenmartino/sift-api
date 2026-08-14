"""One call shape, two provider SDKs.

WHY THIS EXISTS
---------------
Nine of the eleven call sites do the same thing: one system block, one user
message, a `max_tokens` ceiling, JSON-ish text back. They each construct their
own `anthropic.AsyncAnthropic` inline, which is fine while the answer is always
Anthropic and is the blocker the moment a candidate needs to be measured.

WHY NOT LiteLLM
---------------
This repo has twice been burned by a cost number that was wrong and
unqueryable — STATUS.md carried "~$15/mo" against a real ~$300/mo — and
responded by building a cross-repo, cross-language golden-cost parity test.
Delegating the price table to a dependency that updates on `pip install -U`
reopens exactly that hole, and pinning it means re-asserting the prices here
anyway: the in-repo table, plus a dependency, plus a layer of indirection.

The second reason is cultural. `index_alignment` refuses to trust a batch that
has not proven itself; `entity_linker_llm` returns `None` rather than `[]` so
callers can tell "no answer" from "no entities"; `cost_guard` fails closed. A
router that transparently retries somewhere else is adversarial to all of it.

So: `anthropic.AsyncAnthropic` for Anthropic, `openai.AsyncOpenAI` with a
`base_url` for everything else — which covers Together, Groq, DeepInfra,
Fireworks, OpenRouter and OpenAI itself, because they all speak the same wire
format.

WHAT IS DELIBERATELY NOT NORMALIZED
-----------------------------------
`compare.search_sources` (Anthropic's server-side web_search tool) and
`services/batch_client.py` (Message Batches) stay on the anthropic SDK. Neither
has a portable equivalent, and `model_registry.CAPABILITIES` already refuses to
move those operations.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from services import usage_tracker
from services.model_registry import ModelSpec, resolve

logger = logging.getLogger("sift-api.llm_client")


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # A SUBSET of output_tokens, not an addition to it — providers report
    # reasoning inside the completion count, so adding it would double-count.
    #
    # It is reported because it is billed at the output rate and is invisible
    # in the text. Measured 2026-08-13 on a task whose visible answer is 50
    # characters: gpt-5-nano spent 256, DeepSeek V4 Flash 48, Haiku 0 (not a
    # reasoning model). Output is ~56% of this pipeline's bill, so a candidate
    # that reasons before answering costs materially more than its published
    # output rate implies — and scripts/project_model_cost.py, which re-prices
    # the INCUMBENT's measured token counts, cannot see that at all.
    #
    # The operational half is worse than the cost half: every max_tokens
    # ceiling in this repo was fitted to Haiku's verbosity (summarizer 700,
    # linker 500, synthesis 400+120n). Reasoning consumes that budget FIRST, so
    # a ceiling that is generous for Haiku can leave a reasoning model with
    # nothing left to answer with — which arrives as an empty string that fails
    # index_alignment, not as an error.
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    # Normalized to the Anthropic vocabulary — see _normalize_stop_reason.
    stop_reason: str
    spec: ModelSpec
    latency_ms: float
    raw: Any = None


class LLMClientError(RuntimeError):
    """A provider call failed. Deliberately not caught here.

    Every caller in this repo already has a degradation contract — truncated
    RSS text, regex linking, a first-article copy. Swallowing the error here
    would take that decision away from the code that knows what a safe failure
    looks like for its own stage.
    """


def _normalize_stop_reason(raw: str | None) -> str:
    """Map a provider's finish reason onto the vocabulary this repo records.

    `migrations/021_llm_output_stops.sql` and `story_clusterer`'s truncation
    warning both key on the Anthropic strings. Left unnormalized, that table
    would silently stop recording truncation the moment a stage changed
    provider — and truncation is the thing it was built to detect, because a
    response cut at the cap is truncated JSON and fails alignment exactly the
    way a scrambled one does.
    """
    if raw is None:
        return "unknown"
    return {
        # OpenAI-compatible
        "length": "max_tokens",
        "stop": "end_turn",
        "content_filter": "refusal",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        # Anthropic passes through unchanged
        "max_tokens": "max_tokens",
        "end_turn": "end_turn",
        "stop_sequence": "stop_sequence",
        "refusal": "refusal",
        "tool_use": "tool_use",
    }.get(raw, raw)


def _api_key(spec: ModelSpec) -> str:
    key = getattr(settings, spec.api_key_setting, "") or ""
    if not key:
        raise LLMClientError(
            f"{spec.catalog_id} needs {spec.api_key_setting.upper()} and it is "
            f"empty. Set it in sift-api/.env for eval runs. Do NOT set it on "
            f"Railway until a swap actually ships — production has no reason to "
            f"hold a key for a provider it does not call."
        )
    return key


_clients: dict[tuple[str, str | None], Any] = {}


def _client_for(spec: ModelSpec) -> Any:
    """One client per (provider, base_url), reused across calls."""
    cache_key = (spec.provider, spec.base_url)
    if cache_key in _clients:
        return _clients[cache_key]

    if spec.provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=_api_key(spec), max_retries=spec.max_retries
        )
    else:
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as e:  # pragma: no cover - import guard
            raise LLMClientError(
                "the `openai` package is required for non-Anthropic providers"
            ) from e

        client = AsyncOpenAI(
            api_key=_api_key(spec),
            base_url=spec.base_url,
            max_retries=spec.max_retries,
        )

    _clients[cache_key] = client
    return client


async def complete(
    *,
    operation: str,
    user: str,
    system: str | None = None,
    max_tokens: int,
    temperature: float | None = None,
    cache_system: bool = False,
    timeout: float | None = None,
    spec: ModelSpec | None = None,
    client: Any | None = None,
) -> LLMResponse:
    """One turn against whichever model serves `operation`.

    `temperature` defaults to the SPEC's value rather than the provider's.
    Nothing in this repo has ever set it, so every call has run at whatever
    default the vendor chose — and `services/index_alignment` states as a
    premise that "sampling is non-zero temperature, so the retry is a genuinely
    different draw". That premise was inherited, not enforced, and it does not
    transfer: Anthropic's range is 0-1 and OpenAI's is 0-2, so "1.0" is the
    ceiling on one and the midpoint on the other. If a candidate landed on a
    near-deterministic decode, the alignment re-ask would become an exact
    replay of a failing call at full price.
    """
    spec = spec or resolve(operation)
    if temperature is None:
        temperature = spec.default_temperature

    started = time.perf_counter()
    try:
        if spec.provider == "anthropic":
            resp = await _complete_anthropic(
                spec, client, user, system, max_tokens, temperature,
                cache_system, timeout,
            )
        else:
            resp = await _complete_openai_compatible(
                spec, client, user, system, max_tokens, temperature, timeout,
            )
    except Exception as e:
        raise LLMClientError(f"{operation} on {spec.model} failed: {e}") from e

    return LLMResponse(**resp, spec=spec,
                       latency_ms=(time.perf_counter() - started) * 1000)


async def _complete_anthropic(
    spec, client, user, system, max_tokens, temperature, cache_system, timeout,
) -> dict:
    client = client or _client_for(spec)

    kwargs: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": user}],
    }
    if system is not None:
        block: dict[str, Any] = {"type": "text", "text": system}
        # A hint, not a contract: no-ops on providers without prompt caching.
        # Worth knowing it rarely fires here anyway — Haiku 4.5's minimum
        # cacheable prefix is 4,096 tokens and this repo's largest static
        # header is ~1,450.
        if cache_system and spec.supports_prompt_cache:
            block["cache_control"] = {"type": "ephemeral"}
        kwargs["system"] = [block]
    if timeout is not None:
        kwargs["timeout"] = timeout

    resp = await client.messages.create(**kwargs)
    u = getattr(resp, "usage", None)
    return {
        "text": "".join(b.text for b in resp.content if getattr(b, "type", "") == "text"),
        "usage": LLMUsage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        ),
        "stop_reason": _normalize_stop_reason(getattr(resp, "stop_reason", None)),
        "raw": resp,
    }


async def _complete_openai_compatible(
    spec, client, user, system, max_tokens, temperature, timeout,
) -> dict:
    client = client or _client_for(spec)

    messages = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    kwargs: dict[str, Any] = {
        "model": spec.model,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout

    # Deliberately NOT response_format={"type": "json_object"}. It forces a
    # top-level object, and every parser in this repo — _extract_json_array in
    # summarizer, context_generator, entity_linker_llm — wants an array. That
    # is silent breakage for a marginal gain.
    resp = await client.chat.completions.create(**kwargs)

    choice = resp.choices[0] if resp.choices else None
    u = getattr(resp, "usage", None)
    cached = 0
    details = getattr(u, "prompt_tokens_details", None) if u else None
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)

    prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0) if u else 0
    completion_details = getattr(u, "completion_tokens_details", None) if u else None
    reasoning = int(getattr(completion_details, "reasoning_tokens", 0) or 0) \
        if completion_details else 0
    return {
        "text": (getattr(choice.message, "content", "") or "") if choice else "",
        "usage": LLMUsage(
            # Uncached input only, so `input_tokens + cache_read_tokens` means
            # the same thing on both providers. OpenAI reports prompt_tokens as
            # the TOTAL including cached; Anthropic reports them separately.
            input_tokens=max(0, prompt_tokens - cached),
            output_tokens=int(getattr(u, "completion_tokens", 0) or 0) if u else 0,
            cache_read_tokens=cached,
            # No OpenAI-compatible provider bills a cache WRITE premium, so
            # there is nothing to report. Left at 0 rather than guessed.
            cache_write_tokens=0,
            reasoning_tokens=reasoning,
        ),
        "stop_reason": _normalize_stop_reason(
            getattr(choice, "finish_reason", None) if choice else None
        ),
        "raw": resp,
    }


def log_llm_usage(operation: str, resp: LLMResponse, *, web_searches: int = 0) -> dict:
    """Record an adapter response's cost and tokens.

    A separate entry point rather than teaching `log_usage` a second shape.
    `log_usage` reads its fields off an `anthropic.types.Message` with
    `getattr`; hand it an OpenAI response and all four reads return 0, cost
    computes to 0, and `_record_to_ledger` returns early at `cost_usd <= 0` —
    recording NOTHING while looking like data. That is verbatim the failure
    `log_batch_usage`'s own docstring warns about, and an `LLMResponse` can
    only be built by this module, so a raw provider object cannot reach the
    ledger by that route.
    """
    return usage_tracker.log_usage(
        operation,
        _LedgerView(resp),
        model=resp.spec.model,
        web_searches=web_searches,
    )


class _LedgerView:
    """Adapts an LLMResponse to the attribute shape log_usage reads."""

    def __init__(self, resp: LLMResponse) -> None:
        u = resp.usage
        self.usage = type("U", (), {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": u.cache_read_tokens,
            "cache_creation_input_tokens": u.cache_write_tokens,
        })()
        self.stop_reason = resp.stop_reason
        self.model = resp.spec.model
        self.content: list = []
