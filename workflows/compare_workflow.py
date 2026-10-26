from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import NotRequired, TypedDict

import anthropic
from langgraph.graph import StateGraph, END

from app.config import settings
from app.db import get_pool
from services.cost_guard import budget_snapshot
from services.paid_calls import BudgetUnavailable, budgeted_message, budget_scope, message_bound
from services.model_registry import resolve
from services.usage_tracker import count_web_searches, log_usage, prices_for

logger = logging.getLogger("sift-api.compare")

# Two operations live in this file and they are not interchangeable.
# `compare.search_sources` uses Anthropic's server-side web_search_20250305
# tool, which has no portable equivalent — model_registry.CAPABILITIES refuses
# to move it off a model that provides one. `compare.extract_and_compare` is an
# ordinary completion over the text those searches returned, and is swappable.
SEARCH_OPERATION = "compare.search_sources"
EXTRACT_OPERATION = "compare.extract_and_compare"
PER_SOURCE_TIMEOUT = 20  # seconds per source search

# Safe fallback allowlist — used ONLY when the curated set can't be loaded from
# the DB (local dev, startup race, or an outage). The production allowlist is
# data-backed (see load_allowed_sources): it tracks the curated outlet_profiles
# set + source_name_aliases, so the compare source pool stays in sync with the
# outlets Sift actually ingests instead of drifting from a hand-maintained list.
FALLBACK_ALLOWED_SOURCES = {
    "reuters", "bbc", "associated press", "ap news", "npr", "cnn", "fox news",
    "nbc news", "abc news", "cbs news", "the new york times", "washington post",
    "the guardian", "al jazeera", "politico", "the hill", "axios", "bloomberg",
    "cnbc", "financial times", "the economist", "the wall street journal",
    "techcrunch", "the verge", "wired", "ars technica", "nature", "science",
}


async def load_allowed_sources() -> set[str]:
    """Allowed compare source names (lowercased), derived from curated outlets.

    Sources are validated against the curated outlet set rather than a static
    constant: canonical names and slugs from ``outlet_profiles`` plus the raw
    RSS names in ``source_name_aliases``. This keeps the compare allowlist in
    sync with the outlets Sift ingests — including curated right-leaning outlets
    the old hardcoded list omitted. Falls back to ``FALLBACK_ALLOWED_SOURCES``
    (small, safe) if the DB is unavailable or unseeded, so validation stays
    strict and deterministic rather than failing open.
    """
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT LOWER(name) AS s FROM outlet_profiles
            UNION
            SELECT LOWER(slug) AS s FROM outlet_profiles
            UNION
            SELECT LOWER(raw_source_name) AS s FROM source_name_aliases
            """
        )
        allowed = {r["s"].strip() for r in rows if r["s"] and r["s"].strip()}
        if allowed:
            return allowed
        logger.warning(
            "outlet_profiles/source_name_aliases empty; using fallback allowlist"
        )
    except Exception as e:
        logger.warning(
            "Could not load compare allowlist from DB (%s); using fallback", e
        )
    return set(FALLBACK_ALLOWED_SOURCES)


def filter_allowed_sources(sources: list[str], allowed: set[str]) -> list[str]:
    """Keep only sources whose normalized (lowercased, trimmed) name is allowed.

    Matching is case-/whitespace-insensitive; the original source string is
    preserved for the downstream search prompt. Anything not explicitly on the
    allowlist (arbitrary domains, injected strings) is rejected.
    """
    return [s for s in sources if s.lower().strip() in allowed]


def _sanitize_text(text: str) -> str:
    """Strip control characters and collapse whitespace to mitigate prompt injection."""
    # Remove control characters except normal whitespace
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Collapse multiple whitespace to single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


class CompareState(TypedDict):
    topic: str
    sources: list[str]
    search_results: dict[str, str]  # source_name -> coverage text
    claims: list[dict]
    comparison: str
    errors: list[str]
    analyzed_sources: NotRequired[list[str]]


def comparison_provenance(state: CompareState) -> dict:
    """Source availability comes from workflow state, never generated prose."""
    requested = list({s.strip().casefold(): s.strip() for s in state["sources"]}.values())
    found = {s.strip().casefold() for s in state.get("search_results", {})}
    completed = {s.strip().casefold() for s in state.get("analyzed_sources", [])}
    retrieved = [s for s in requested if s.casefold() in found]
    analyzed = [s for s in retrieved if s.casefold() in completed]
    return {
        "source_provenance_version": 1,
        "sources_requested": requested,
        "sources_retrieved": retrieved,
        "sources_analyzed": analyzed,
        "sources_unavailable": [s for s in requested if s not in retrieved],
    }


async def compare_search_parallelism(source_count: int) -> int:
    """Reuse capacity as searches settle rather than requiring five max holds.

    This snapshot chooses concurrency only; atomic per-call admission remains
    authoritative when requests race. Keep extraction headroom where possible.
    """
    if not settings.ai_cost_guard_enabled:
        return max(1, source_count)
    try:
        budget = await budget_snapshot()
    except Exception as exc:
        raise BudgetUnavailable("Budget cannot be verified", "COST_GUARD_UNAVAILABLE") from exc
    cap = budget["daily_limit_usd"]
    if not budget["enabled"] or cap <= 0:
        return max(1, source_count)
    if budget_scope.get() != "compare":
        cap *= 1 - budget["compare_share"]
    search_bound = message_bound({"model": resolve(SEARCH_OPERATION).model, "max_tokens": 2048,
        "messages": [], "tools": [{"name": "web_search", "max_uses": 1}]})
    rates = prices_for(resolve(EXTRACT_OPERATION).model)
    extract_headroom = (200_000 * max(rates.input_per_m, rates.cache_write_per_m) + 4096 * rates.output_per_m) / 1_000_000
    available = cap - budget["spent_usd"] - budget["held_usd"] - extract_headroom
    return max(1, min(source_count, int(max(0, available) / search_bound)))


# --- Node functions ---


async def search_one_source(
    client: "anthropic.AsyncAnthropic", topic: str, source: str
) -> tuple[str, str | None]:
    """Search a single source. Returns (source_name, result_text | None).

    Module-level (not a closure inside the node) so the SSE stream endpoint
    can fan these out with as_completed and emit a per-source event the
    moment each one finishes — the same work, reported live.
    """
    try:
        response = await asyncio.wait_for(
            budgeted_message(client, SEARCH_OPERATION,
                model=resolve(SEARCH_OPERATION).model,
                max_tokens=2048,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 1,
                }],
                messages=[{
                    "role": "user",
                    "content": (
                        f'Search for recent news coverage from {source} about: "{topic}"\n\n'
                        f"Summarize what {source} reports about this topic, including key facts, "
                        f"figures, quotes, and their perspective or framing. "
                        f"If you cannot find relevant coverage from {source}, say "
                        f"'No relevant coverage found from {source}.'"
                    ),
                }],
            ),
            timeout=PER_SOURCE_TIMEOUT,
        )
        log_usage(
            SEARCH_OPERATION,
            response,
            model=resolve(SEARCH_OPERATION).model,
            web_searches=count_web_searches(response),
        )

        # Extract text blocks from response
        text_parts = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)

        result = "\n".join(text_parts).strip()
        if not result:
            return (source, None)

        logger.info("search_sources: got %d chars from %s", len(result), source)
        return (source, result)

    except BudgetUnavailable:
        raise
    except asyncio.TimeoutError:
        logger.warning("search_sources: timed out for %s after %ds", source, PER_SOURCE_TIMEOUT)
        return (source, None)
    except Exception as e:
        logger.error("search_sources: failed for %s: %s", source, e)
        return (source, None)


def source_found_coverage(text: str | None) -> bool:
    """Whether a search_one_source result counts as real coverage."""
    if not text or not text.strip():
        return False
    missing = r"no (?:relevant )?(?:coverage|articles?) (?:was |were )?found|coverage (?:was |is )?unavailable"
    return re.search(missing, text, re.I) is None


async def search_sources_node(state: CompareState) -> dict:
    """Search each source in parallel using Claude web_search."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
    topic = _sanitize_text(state["topic"])
    # Validate sources against the curated allowlist; reject unknown names.
    allowed = await load_allowed_sources()
    sources = filter_allowed_sources(state["sources"], allowed)
    if not sources:
        return {
            "search_results": {},
            "errors": ["No valid sources provided"],
        }

    gate = asyncio.Semaphore(await compare_search_parallelism(len(sources)))
    async def search(source):
        async with gate:
            return await search_one_source(client, topic, source)
    results = await asyncio.gather(
        *[search(s) for s in sources],
        return_exceptions=True,
    )

    search_results: dict[str, str] = {}
    errors: list[str] = list(state.get("errors", []))

    for result in results:
        if isinstance(result, BudgetUnavailable):
            raise result
        if isinstance(result, Exception):
            errors.append(f"search: {result}")
            continue
        source_name, text = result
        if source_found_coverage(text):
            search_results[source_name] = text  # type: ignore[assignment]
        else:
            errors.append(f"No coverage found from {source_name}")

    logger.info(
        "search_sources: got results from %d/%d sources",
        len(search_results),
        len(sources),
    )

    return {"search_results": search_results, "errors": errors}


async def extract_and_compare_node(state: CompareState) -> dict:
    """Extract claims from search results and compare across sources."""
    search_results = state.get("search_results", {})

    if not search_results:
        return {
            "comparison": "Could not find relevant coverage from any source.",
            "claims": [],
        }

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    # Format source texts for the prompt
    source_texts = ""
    for source, text in search_results.items():
        source_texts += f"\n--- {source.upper()} ---\n{text}\n"

    sources_list = list(search_results.keys())

    response = await budgeted_message(client, EXTRACT_OPERATION,
        model=resolve(EXTRACT_OPERATION).model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"""Analyze the following news coverage of "{_sanitize_text(state['topic'])}" from multiple sources.

{source_texts}

Your task:
Only compare the {len(sources_list)} successfully retrieved sources in Available sources below.
Do not mention or count any other outlet. If only one source is available, there is no multi-source agreement.
1. Extract 3-8 key factual claims made across these sources.
2. For each claim, determine the agreement level:
   - "unanimous": every available source reports this claim (at least two sources)
   - "majority": more than half of available sources report this claim
   - "partial": at least two, but no more than half, report this claim
   - "disputed": sources contradict each other on this point
   - "unique": only one source reports this
3. Write a 2-3 sentence overall comparison summary describing how coverage differs or aligns.

Return ONLY a JSON object with this structure:
{{
  "comparison": "2-3 sentence summary of how the sources compare...",
  "claims": [
    {{
      "claim": "specific factual statement",
      "agreement": "unanimous",
      "sources": ["source1", "source2"]
    }},
    {{
      "claim": "a disputed point",
      "agreement": "disputed",
      "sources_for": ["source1"],
      "sources_against": ["source2"]
    }}
  ]
}}

Available sources: {json.dumps(sources_list)}
Return ONLY the JSON, no other text.""",
        }],
    )
    log_usage(EXTRACT_OPERATION, response, model=resolve(EXTRACT_OPERATION).model)

    # Extract text from response
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    # Parse JSON from response
    parsed = _extract_json_object(text)

    if not parsed:
        logger.error("Failed to parse comparison JSON: %s", text[:500])
        return {
            "comparison": text.strip()[:500] if text.strip() else "Comparison analysis failed.",
            "claims": [],
            "errors": state.get("errors", []) + ["Failed to parse comparison JSON"],
        }

    comparison = parsed.get("comparison", "")
    claims = parsed.get("claims", [])
    if not isinstance(comparison, str) or not isinstance(claims, list):
        return {"comparison": "", "claims": [], "analyzed_sources": []}

    logger.info("extract_and_compare: got %d claims", len(claims))
    return {"comparison": comparison, "claims": claims, "analyzed_sources": sources_list}


async def format_response_node(state: CompareState) -> dict:
    """Validate and clean up the response (no LLM call)."""
    claims = state.get("claims", [])
    cleaned_claims: list[dict] = []
    provenance = comparison_provenance(state)
    analyzed = provenance["sources_analyzed"]
    names = {s.casefold(): s for s in analyzed}

    def known_sources(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            names[s.strip().casefold()] for s in value
            if isinstance(s, str) and s.strip().casefold() in names
        ))

    for claim in claims:
        if not isinstance(claim, dict) or "claim" not in claim:
            continue

        cleaned: dict = {
            "claim": str(claim["claim"]),
            "agreement": claim.get("agreement", "unique"),
        }

        # Ensure agreement is valid
        if cleaned["agreement"] not in ("unanimous", "majority", "partial", "disputed", "unique"):
            cleaned["agreement"] = "unique"

        # Normalize source fields
        if cleaned["agreement"] == "disputed":
            supporters = known_sources(claim.get("sources_for", []))
            opponents = known_sources(claim.get("sources_against", []))
            overlap = set(supporters) & set(opponents)
            cleaned["sources_for"] = [s for s in supporters if s not in overlap]
            cleaned["sources_against"] = [s for s in opponents if s not in overlap]
            cleaned["sources"] = []
            if not cleaned["sources_for"] or not cleaned["sources_against"]:
                continue
        else:
            cleaned["sources"] = known_sources(claim.get("sources", []))
            cleaned["sources_for"] = []
            cleaned["sources_against"] = []
            count = len(cleaned["sources"])
            if not count:
                continue
            cleaned["agreement"] = (
                "unique" if count == 1 else "unanimous" if count == len(analyzed)
                else "majority" if count > len(analyzed) / 2 else "partial"
            )

        cleaned_claims.append(cleaned)

    # Disputed first — cross-spectrum disagreement is the feature's whole
    # point, so it leads instead of sitting mid-list. Stable sort keeps the
    # model's ordering within each band.
    agreement_order = {"disputed": 0, "majority": 1, "unanimous": 2, "partial": 3, "unique": 4}
    cleaned_claims.sort(key=lambda c: agreement_order.get(c["agreement"], 4))

    comparison = state.get("comparison", "") if analyzed else ""
    unavailable = [s for s in provenance["sources_requested"] if s not in analyzed]
    numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    count_claims = re.findall(r"\ball\s+(one|two|three|four|five|\d+)\s+sources?\b", comparison, re.I)
    wrong_count = any(numbers.get(n.lower(), int(n) if n.isdigit() else 0) != len(analyzed)
                      for n in count_claims)
    mentions_unavailable = any(re.search(r"(?<!\w)" + re.escape(s) + r"(?!\w)", comparison, re.I)
                               for s in unavailable)
    if analyzed and (wrong_count or mentions_unavailable):
        comparison = f"Compared coverage from {', '.join(analyzed)}. Review the sourced claims below."
    return {"claims": cleaned_claims, "comparison": comparison}


# --- JSON parsing helpers ---


def _extract_json_object(text: str) -> dict | None:
    """Extract a JSON object from potentially messy LLM output."""
    text = text.strip()

    # Strategy 1: direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown fences
    cleaned = re.sub(r"```json\n?", "", text)
    cleaned = re.sub(r"```\n?", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 3: find { ... } brackets
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


# --- Build the graph ---


def build_compare_graph():
    """Build and compile the LangGraph comparison workflow."""
    graph = StateGraph(CompareState)

    graph.add_node("search_sources", search_sources_node)
    graph.add_node("extract_and_compare", extract_and_compare_node)
    graph.add_node("format_response", format_response_node)

    graph.set_entry_point("search_sources")
    graph.add_edge("search_sources", "extract_and_compare")
    graph.add_edge("extract_and_compare", "format_response")
    graph.add_edge("format_response", END)

    return graph.compile()
