"""Which model serves which pipeline stage.

WHY THIS EXISTS
---------------
Eleven modules each hardcoded `MODEL = "claude-haiku-4-5-20251001"` as a
module-level constant, so "run one stage on a different model" was a code
change in a file per stage, a PR, and a deploy. That is fine while the answer
is always Haiku and useless the moment you want to measure an alternative.

It is worth being blunt about why that question is live: **Haiku 4.5 is the
cheapest Claude there is**. There is no cheaper tier to drop to, so a
cost-motivated model change means leaving Anthropic for some stages — which is
a real experiment with a real quality risk, and one that cannot even be
attempted while the model id is a constant in eleven files.

THE KEY IS THE OPERATION ID, NOT A NEW VOCABULARY
-------------------------------------------------
`log_usage("summarizer.batch", ...)` already names every call site, and
`ai_usage_daily`'s primary key is `(usage_date, provider, model, operation)`.
Cost, output-stop counts and model selection therefore all key on the same
string, and a stage's spend can be read straight off the ledger under the model
that produced it. Inventing a parallel set of names here would have meant a
mapping table between two vocabularies that must never disagree.

`tests/test_model_registry.py` walks the AST of `services/`, `workflows/` and
`app/` for the literal first argument of every `log_usage`/`log_batch_usage`
call and asserts the set equals `OPERATIONS`. Without that, a stage added later
is silently un-overridable and a typo'd operation id in an override is silently
ignored — and a silently-ignored override is the worst failure this module can
have, because the experiment still runs, still reports a number, and the number
describes the incumbent.

WHAT RESOLVE DOES NOT DO
------------------------
It does not pick a *better* model, and it has no fallback. Every stage defaults
to the incumbent that was hardcoded before this module existed, so an empty
`LLM_MODEL_OVERRIDES` reproduces today's behaviour exactly.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger("sift-api.model_registry")


@dataclass(frozen=True)
class ModelSpec:
    """One model, and what it can be asked to do.

    The capability flags are not documentation — `resolve` refuses an override
    that would put an operation on a model lacking what that operation needs.
    See CAPABILITIES below for why each one can fail silently otherwise.
    """

    catalog_id: str  # repo-local alias used in LLM_MODEL_OVERRIDES
    provider: str  # "anthropic"
    model: str  # the id sent on the wire, and written to ai_usage_daily
    supports_batch: bool  # a Message Batches-style async API at a discount
    supports_prompt_cache: bool
    supports_server_web_search: bool


# Both entries are Anthropic today. The catalog exists so a candidate can be
# added as data rather than as a code change scattered across stages.
MODELS: dict[str, ModelSpec] = {
    "haiku-4-5": ModelSpec(
        catalog_id="haiku-4-5",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        supports_batch=True,
        supports_prompt_cache=True,
        supports_server_web_search=True,
    ),
    "sonnet-4-6": ModelSpec(
        catalog_id="sonnet-4-6",
        provider="anthropic",
        model="claude-sonnet-4-6",
        supports_batch=True,
        supports_prompt_cache=True,
        supports_server_web_search=True,
    ),
}


# Capabilities an operation cannot run without.
#
# BATCH: context/primer/entity_extractor submit through the Message Batches API
# and return immediately, with services/batch_poller.py landing the results
# later. A model without an equivalent does not merely cost more — it changes
# the control flow, moving three stages back in-band against the 30-minute
# REFRESH_INTERVAL. It also surrenders the 50% batch discount, so the true
# price comparison for these three is 2x what a list-price table suggests.
#
# SERVER_WEB_SEARCH: compare.search_sources uses Anthropic's server-side
# web_search_20250305 tool. There is no portable equivalent, and
# usage_tracker.count_web_searches reads `server_tool_use` blocks to bill it.
BATCH = "batch"
SERVER_WEB_SEARCH = "server_web_search"

CAPABILITIES: dict[str, frozenset[str]] = {
    "context_generator.batch": frozenset({BATCH}),
    "primer_generator.batch": frozenset({BATCH}),
    "entity_extractor.batch": frozenset({BATCH}),
    "compare.search_sources": frozenset({SERVER_WEB_SEARCH}),
}


# Every operation the pipeline logs spend against, and the model it ran on
# before this module existed. Changing a value here changes production; the
# override env var is the way to change one temporarily.
INCUMBENT_BY_OPERATION: dict[str, str] = {
    "summarizer.batch": "haiku-4-5",
    "context_generator.batch": "haiku-4-5",
    "primer_generator.batch": "haiku-4-5",
    "entity_extractor.batch": "haiku-4-5",
    "entity_linker_llm.link_text": "haiku-4-5",
    "story_clusterer.cluster": "haiku-4-5",
    "story_synthesizer.synthesize": "haiku-4-5",
    "story_confirmer.confirm": "haiku-4-5",
    "compare.search_sources": "haiku-4-5",
    "compare.extract_and_compare": "haiku-4-5",
    # The only non-Haiku stage: an adjudicator has to be better than the thing
    # it judges, or it just re-votes for whichever answer Haiku preferred.
    "judge.batch": "sonnet-4-6",
}

OPERATIONS: frozenset[str] = frozenset(INCUMBENT_BY_OPERATION)

# Where an unknown operation lands. Haiku rather than Sonnet: a mistake that
# costs 1x is recoverable, one that costs 3x on the highest-volume path is the
# class of error docs/SOURCE_SCALING.md exists because of.
_DEFAULT_CATALOG_ID = "haiku-4-5"

# services/embedder.py is deliberately absent. It calls Voyage, not a chat
# model, and records to the ledger under provider 'voyage' via cost_guard
# directly rather than through log_usage. Nothing here would apply to it.


@functools.lru_cache(maxsize=8)
def _parse_overrides(raw: str) -> dict[str, str]:
    """Parse "op=catalog_id,op=catalog_id" into a mapping.

    Every rejection logs and drops that entry rather than raising: a malformed
    override must not take the service down at import, and the incumbent is
    always a safe thing to fall back to. Cached on the raw string so `resolve`
    stays cheap on a per-call path — tests vary the string, so they get fresh
    parses without touching the cache.
    """
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        if "=" not in entry:
            logger.error(
                "model_registry: ignoring malformed override %r "
                "(expected operation=catalog_id)",
                entry,
            )
            continue
        operation, _, catalog_id = entry.partition("=")
        operation, catalog_id = operation.strip(), catalog_id.strip()
        if operation not in OPERATIONS:
            logger.error(
                "model_registry: ignoring override for unknown operation %r — "
                "known operations are %s",
                operation,
                ", ".join(sorted(OPERATIONS)),
            )
            continue
        if catalog_id not in MODELS:
            logger.error(
                "model_registry: ignoring override %s=%r — unknown model; "
                "known models are %s",
                operation,
                catalog_id,
                ", ".join(sorted(MODELS)),
            )
            continue
        out[operation] = catalog_id
    return out


def resolve(operation: str) -> ModelSpec:
    """The model that should serve `operation` right now.

    Call this in the function body, never at import: a module-level
    `MODEL = resolve(...)` would freeze the value at import time, so an
    override would need a restart to take effect *and* the constant would lie
    in any test that varies the setting.

    An unknown operation, or an override the operation's capabilities forbid,
    falls back to the incumbent and logs at ERROR. Silence here is the one
    thing this module must not do — a dropped override means the A/B runs the
    incumbent against itself and reports the result as a candidate's.
    """
    incumbent = INCUMBENT_BY_OPERATION.get(operation)
    if incumbent is None:
        logger.error(
            "model_registry: unknown operation %r — falling back to %s. "
            "Add it to INCUMBENT_BY_OPERATION.",
            operation,
            _DEFAULT_CATALOG_ID,
        )
        return MODELS[_DEFAULT_CATALOG_ID]

    chosen = _parse_overrides(settings.llm_model_overrides).get(operation)
    if chosen is None or chosen == incumbent:
        return MODELS[incumbent]

    spec = MODELS[chosen]
    required = CAPABILITIES.get(operation, frozenset())
    missing = sorted(c for c in required if not _supports(spec, c))
    if missing:
        logger.error(
            "model_registry: refusing override %s=%s — model lacks %s. "
            "Staying on %s.",
            operation,
            chosen,
            ", ".join(missing),
            incumbent,
        )
        return MODELS[incumbent]
    return spec


def _supports(spec: ModelSpec, capability: str) -> bool:
    return {
        BATCH: spec.supports_batch,
        SERVER_WEB_SEARCH: spec.supports_server_web_search,
    }.get(capability, False)


def assignments() -> dict[str, str]:
    """operation -> wire model id, as currently resolved.

    Surfaced on /health and logged at startup so "which model is this stage
    actually on" has a direct answer from outside the process. CLAUDE.md makes
    the same point about deploys: do not infer a change is live, read it.
    """
    return {op: resolve(op).model for op in sorted(OPERATIONS)}


def non_default_assignments() -> dict[str, str]:
    """Only the stages not running their incumbent — usually empty."""
    return {
        op: resolve(op).catalog_id
        for op in sorted(OPERATIONS)
        if resolve(op).catalog_id != INCUMBENT_BY_OPERATION[op]
    }
