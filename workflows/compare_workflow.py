from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TypedDict

import anthropic
from langgraph.graph import StateGraph, END

from app.config import settings

logger = logging.getLogger("sift-api.compare")

MODEL = "claude-haiku-4-5-20251001"
PER_SOURCE_TIMEOUT = 20  # seconds per source search

# Allowed source names — reject anything not on this list
ALLOWED_SOURCES = {
    "reuters", "bbc", "associated press", "ap news", "npr", "cnn", "fox news",
    "nbc news", "abc news", "cbs news", "the new york times", "washington post",
    "the guardian", "al jazeera", "politico", "the hill", "axios", "bloomberg",
    "cnbc", "financial times", "the economist", "the wall street journal",
    "techcrunch", "the verge", "wired", "ars technica", "nature", "science",
}


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


# --- Node functions ---


async def search_sources_node(state: CompareState) -> dict:
    """Search each source in parallel using Claude web_search."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    topic = _sanitize_text(state["topic"])
    # Validate sources against allowlist; reject unknown names
    sources = [
        s for s in state["sources"]
        if s.lower().strip() in ALLOWED_SOURCES
    ]
    if not sources:
        return {
            "search_results": {},
            "errors": ["No valid sources provided"],
        }

    async def search_one(source: str) -> tuple[str, str | None]:
        """Search a single source. Returns (source_name, result_text | None)."""
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=MODEL,
                    max_tokens=2048,
                    tools=[{
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 3,
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

        except asyncio.TimeoutError:
            logger.warning("search_sources: timed out for %s after %ds", source, PER_SOURCE_TIMEOUT)
            return (source, None)
        except Exception as e:
            logger.error("search_sources: failed for %s: %s", source, e)
            return (source, None)

    # Run all source searches in parallel
    results = await asyncio.gather(
        *[search_one(s) for s in sources],
        return_exceptions=True,
    )

    search_results: dict[str, str] = {}
    errors: list[str] = list(state.get("errors", []))

    for result in results:
        if isinstance(result, Exception):
            errors.append(f"search: {result}")
            continue
        source_name, text = result
        if text and "no relevant coverage found" not in text.lower():
            search_results[source_name] = text
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

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Format source texts for the prompt
    source_texts = ""
    for source, text in search_results.items():
        source_texts += f"\n--- {source.upper()} ---\n{text}\n"

    sources_list = list(search_results.keys())

    response = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"""Analyze the following news coverage of "{_sanitize_text(state['topic'])}" from multiple sources.

{source_texts}

Your task:
1. Extract 3-8 key factual claims made across these sources.
2. For each claim, determine the agreement level:
   - "unanimous": all sources that cover this claim agree
   - "majority": most sources agree, some don't cover it
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

    logger.info("extract_and_compare: got %d claims", len(claims))
    return {"comparison": comparison, "claims": claims}


async def format_response_node(state: CompareState) -> dict:
    """Validate and clean up the response (no LLM call)."""
    claims = state.get("claims", [])
    cleaned_claims: list[dict] = []

    for claim in claims:
        if not isinstance(claim, dict) or "claim" not in claim:
            continue

        cleaned: dict = {
            "claim": str(claim["claim"]),
            "agreement": claim.get("agreement", "unique"),
        }

        # Ensure agreement is valid
        if cleaned["agreement"] not in ("unanimous", "majority", "disputed", "unique"):
            cleaned["agreement"] = "unique"

        # Normalize source fields
        if cleaned["agreement"] == "disputed":
            cleaned["sources_for"] = claim.get("sources_for", [])
            cleaned["sources_against"] = claim.get("sources_against", [])
            cleaned["sources"] = []
        else:
            cleaned["sources"] = claim.get("sources", [])
            cleaned["sources_for"] = []
            cleaned["sources_against"] = []

        cleaned_claims.append(cleaned)

    return {"claims": cleaned_claims}


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
