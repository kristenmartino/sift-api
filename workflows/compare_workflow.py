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
from services.compare_coverage import indexed_coverage, publisher_article_hints
from services.compare_search import normalize_evidence_text, publisher_domains, publisher_search_excerpts
from services.compare_publisher_feed import PUBLIC_DESCRIPTION_PREFIX, public_feed_excerpt

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
    stored = await indexed_coverage(topic, source)
    if stored and stored["excerpt"]:
        logger.info("compare indexed coverage: using publisher excerpt for %s", source)
        return (source, stored["excerpt"])
    if stored:
        public_excerpt = await public_feed_excerpt(stored['url'], source)
        if public_excerpt:
            logger.info('compare public publisher feed: using exact original description for %s', source)
            return source, public_excerpt
    article_hint = (f"\nA matching indexed article is {stored['title']} at {stored['url']}. "
                    "Retrieve its reporting; the title alone is not evidence.\n" if stored else "")
    domains = publisher_domains(source)
    hints = await publisher_article_hints(topic) if not stored and domains else []
    if hints:
        article_hint += ('\nPossible publisher articles (discovery hints only; verify the subject and timing):\n'
                         + '\n'.join(f"Candidate {i}: {h['title']} at {h['url']}\n{h.get('excerpt', '')}" for i, h in enumerate(hints))
                         + '\nDo not treat a suggested title or URL as reporting.\n')
        article_hint += (
            'Some candidates include original publisher excerpts. If such a candidate genuinely covers '
            'the requested subject and timing, its excerpt can supply limited reporting or an attributed '
            'opinion even when the page is blocked. Do not select a merely related event or passing reference. '
            'Confirm selection with a standalone line SELECTED_STORED_CANDIDATE=N using its candidate number. '
            'Never select a candidate without an excerpt. You may search once if needed.\n'
        )
    search_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
    if domains:
        search_tool['allowed_domains'] = list(domains)
    target = stored['title'] if stored else hints[0]['title'] if hints else topic
    query = f'site:{domains[0]} {target}' if domains else f'{source} {target}'
    try:
        response = await asyncio.wait_for(
            budgeted_message(client, SEARCH_OPERATION,
                stream_response=True,
                model=resolve(SEARCH_OPERATION).model,
                max_tokens=2048,
                tools=[search_tool],
                messages=[{
                    "role": "user",
                    "content": (
                        f'Search for recent news coverage from {source} about: "{topic}"\n\n'
                        f"{article_hint}"
                        f"Use one targeted search query: {query}\n"
                        "Match the requested subject and timing; do not substitute a related but different event. "
                        "Cite the selected publisher's reporting. Public search-index passages may supply "
                        "reporting when a direct page request is blocked; a blocked page alone does not prove "
                        "the outlet has no coverage. Titles, URLs and subscription messages alone are not reporting. "
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

        selected = re.search(r'^`{0,3}SELECTED_STORED_CANDIDATE=(\d+)`{0,3}\s*$', result, re.M)
        if hints and selected and source_found_coverage(result):
            index = int(selected[1])
            if index < len(hints) and hints[index].get('excerpt'):
                logger.info('search_sources: verified original publisher candidate for %s', source)
                return source, hints[index]['excerpt']

        if domains:
            result = (publisher_search_excerpts(response, domains, stored['url'] if stored else None)
                      if source_found_coverage(result) else None)
            if not result:
                logger.info("search_sources: no cited publisher reporting from %s", source)
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

    # A complete short public standfirst is safe to quote, but too limited to
    # turn into inferred article facts or a purported dispute. Keep it out of
    # model grouping and append its checked literal quotation below.
    model_reporting = {s: text for s, text in search_results.items()
                       if not (text.startswith(PUBLIC_DESCRIPTION_PREFIX) and _complete_short_excerpt(text))}
    if not model_reporting:
        claims = _retain_outlet_passages([], None, search_results)
        return {"comparison": _grounded_comparison_summary(claims, list(search_results)),
                "claims": claims, "analyzed_sources": list(search_results)}

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    # Format source texts for the prompt
    source_texts = ""
    for source, text in model_reporting.items():
        source_texts += f"\n--- {source.upper()} ---\n{text}\n"

    sources_list = list(model_reporting)

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
Stored publisher excerpts may be incomplete. Use only facts or explicitly attributed opinions present in the supplied reporting. Missing details in an excerpt do not prove an outlet omitted them from its full article.
First assess EACH available outlet independently in outlet_passages: select one verbatim passage of 6-30 words expressing a relevant, self-contained fact or opinion. Use null when none exists. A complete subject-and-predicate clause within a truncated excerpt is usable; select only that intact clause. Never return an unfinished word or trailing ellipsis, complete missing words, or infer the missing context. Do not use headline/URL metadata, teasers, or fragments without a complete proposition. This assessment comes BEFORE grouping claims, so richer articles do not crowd out a short editorial excerpt.
An article's headline may establish the topic of its excerpt, but cannot supply evidence for a claim. An editorial view about the underlying issue is a relevant contribution even when its excerpt does not repeat the event name. Retain it as that outlet's view without inferring a stance on specific policy details.
For EVERY outlet attributed to a grouped claim, provide a verbatim supporting quotation of 6-30 words from that outlet's supplied reporting. The quotation must support that specific claim. Never borrow evidence from another outlet or from headline/URL metadata. If an excerpt does not support that fact or attributed opinion, do not credit that outlet with it, even when other outlets confirm it.
Select the verbatim supporting passage first, then write only the claim it supports. Do not invent or paraphrase a quotation to fit a claim already written. Where the text supports it, include a factual claim or clearly attributed position from each available outlet; do not silently omit an outlet's supported perspective. An outlet with no supportable passage may be omitted.
1. Extract up to 8 supportable claims. A short excerpt may support only one claim; that is valid. Do not pad to a minimum. Keep an opinion or interpretation explicitly attributed to its outlet instead of presenting it as an established fact. Return no claims only when none is supported by the supplied reporting.
2. For each claim, determine the agreement level:
   - "unanimous": every available source reports this claim (at least two sources)
   - "majority": more than half of available sources report this claim
   - "partial": at least two, but no more than half, report this claim
   - "disputed": sources contradict each other on this point
   - "unique": only one source reports this
3. Include an evidence map for every claim. Unsupported outlet attributions will be removed.

Return ONLY a JSON object with this structure:
{{
  "outlet_passages": {{"source1": "verbatim self-contained passage from source1", "source2": null}},
  "claims": [
    {{
      "evidence": {{"source1": "verbatim supporting quotation from source1 reporting", "source2": "verbatim supporting quotation from source2 reporting"}},
      "claim": "specific factual statement",
      "agreement": "unanimous",
      "sources": ["source1", "source2"]
    }},
    {{
      "evidence": {{"source1": "verbatim quotation supporting the claim", "source2": "verbatim quotation contradicting the claim"}},
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
            "comparison": "Comparison analysis could not be completed. Try again.",
            "claims": [],
            "errors": state.get("errors", []) + ["Failed to parse comparison JSON"],
        }

    claims = parsed.get("claims", [])
    if not isinstance(claims, list):
        return {"comparison": "", "claims": [], "analyzed_sources": []}

    original_count = len(claims)
    claims = _ground_claim_attributions(claims, model_reporting)
    claims = _retain_outlet_passages(claims, parsed.get("outlet_passages"), search_results)
    comparison = _grounded_comparison_summary(claims, list(search_results))
    logger.info("extract_and_compare: %d grouped candidates, %d retained contributions", original_count, len(claims))
    return {"comparison": comparison, "claims": claims, "analyzed_sources": list(search_results)}


def _ground_claim_attributions(claims: list, reporting: dict[str, str]) -> list[dict]:
    """Reject borrowed/invented quotes before counting an outlet for a claim.

    This checks provenance, not factual truth or semantic entailment. The model
    still interprets the quote, but can no longer silently attribute a fact to
    an excerpt without returning supporting text from that excerpt.
    """
    def normalize(text: str) -> str:
        return normalize_evidence_text(text)

    bodies = {}
    for source, text in reporting.items():
        if text.startswith(("Stored publisher excerpt", "Retrieved publisher excerpts", PUBLIC_DESCRIPTION_PREFIX)):
            text = text.partition("\nExcerpt:\n")[2]
        bodies[source.strip().casefold()] = (source, normalize(text))
    grounded = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim"), str):
            continue
        evidence = claim.get("evidence")
        if not isinstance(evidence, dict):
            continue
        quotes = {s.strip().casefold(): q for s, q in evidence.items() if isinstance(s, str)}
        cleaned = {"claim": claim["claim"], "agreement": claim.get("agreement", "unique")}
        for field in ("sources", "sources_for", "sources_against"):
            credited = claim.get(field, [])
            cleaned[field] = []
            for source in credited if isinstance(credited, list) else []:
                if not isinstance(source, str):
                    continue
                key = source.strip().casefold()
                quote = quotes.get(key)
                if (key in bodies and isinstance(quote, str) and len(quote.split()) >= 6
                        and normalize(quote) in bodies[key][1]):
                    name = bodies[key][0]
                    if name not in cleaned[field]:
                        cleaned[field].append(name)
        if cleaned["agreement"] == "disputed":
            if not cleaned["sources_for"] or not cleaned["sources_against"]:
                continue
        elif not cleaned["sources"]:
            continue
        grounded.append(cleaned)
    return grounded


def _complete_short_excerpt(text: str) -> str | None:
    body = text.partition("\nExcerpt:\n")[2].strip()
    if (6 <= len(body.split()) <= 30 and body.endswith((".", "!", "?"))
            and not body.endswith("...")):
        return body
    return None


def _retain_outlet_passages(claims: list[dict], passages, reporting: dict[str, str]) -> list[dict]:
    """Retain a checked quotation when grouping omitted an outlet entirely.

    Quote the passage itself, rather than inventing a fallback paraphrase or
    treating editorial opinion as established fact. This uses the same model
    call and provenance guard; null, metadata and unfinished text add nothing.
    Reserve coverage before filling the eight slots with additional claims.
    """
    candidates = list(claims)

    def credited(claim):
        return set(claim.get("sources", []) + claim.get("sources_for", []) + claim.get("sources_against", []))

    represented = set().union(*(credited(c) for c in candidates))
    passages = ({s.strip().casefold(): q for s, q in passages.items() if isinstance(s, str)}
                if isinstance(passages, dict) else {})
    for source, text in reporting.items():
        quote = passages.get(source.strip().casefold())
        # Original excerpts already passed event-relevance retrieval.
        # Preserve a whole short sentence even if the model overlooks opinion.
        if text.startswith(("Stored publisher excerpt", PUBLIC_DESCRIPTION_PREFIX)):
            quote = _complete_short_excerpt(text) or quote
        if (source in represented or not isinstance(quote, str)
                or not 6 <= len(quote.split()) <= 30 or quote.rstrip().endswith(("...", "…"))):
            continue
        fallback = {"claim": f'{source} writes: “{quote}”', "agreement": "unique",
                    "sources": [source], "evidence": {source: quote}}
        candidates.extend(_ground_claim_attributions([fallback], reporting))

    selected = set()
    covered = set()
    # Keep at least one supported contribution per outlet, then disputes.
    for i, claim in enumerate(candidates):
        sources = credited(claim)
        if len(selected) < 8 and sources - covered:
            selected.add(i)
            covered.update(sources)
    for i, claim in enumerate(candidates):
        if len(selected) < 8 and claim.get("agreement") == "disputed":
            selected.add(i)
    for i in range(len(candidates)):
        if len(selected) >= 8:
            break
        selected.add(i)
    return [c for i, c in enumerate(candidates) if i in selected]


def _grounded_comparison_summary(claims: list[dict], sources: list[str]) -> str:
    """Build the headline comparison from checked attribution counts.

    A separate free-form model summary could reintroduce unsupported 'all
    outlets confirm' claims even after their source lists had been corrected.
    """
    if not claims:
        return "No claims could be tied to supporting passages in the retrieved text."
    shared = [c for c in claims if c.get("agreement") != "disputed" and c.get("sources")]
    if not shared:
        return "Review the claims below for the differences supported by the retrieved reporting."
    broadest = max(shared, key=lambda c: len(c["sources"]))
    statement = broadest["claim"].rstrip(". ")

    def attributed_quotation(claim: dict) -> str | None:
        if (len(claim["sources"]) == 1
                and claim["claim"].startswith(f'{claim["sources"][0]} writes: “')):
            text = claim["claim"].strip()
            return text if text.rstrip('”').endswith((".", "!", "?")) else text + "."
        return None

    quotation = attributed_quotation(broadest)
    if len(sources) == 1:
        return quotation or f"{sources[0]} reports: {statement}."
    summary = quotation or f"{len(broadest['sources'])} of {len(sources)} retrieved outlets report: {statement}."
    unique = next((c for c in shared if len(c["sources"]) == 1 and c is not broadest), None)
    if unique:
        summary += " " + (attributed_quotation(unique)
                          or f"{unique['sources'][0]} also reports: {unique['claim'].rstrip('. ')}.")
    return summary


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
