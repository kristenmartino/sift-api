"""LLM-based entity linker — civic-literacy MVP Phase 3.G.2.

Replaces the regex matcher in services/entity_linker.link_text with a
Claude Haiku call that takes the article + the curated catalog and
returns which entities the article actually mentions.

Why this exists
---------------
The regex linker (Phase 3.G) had a name-collision blind spot. After
PR #40 dropped last-name-only aliases, common-noun false positives
("downing power lines" → Senator Downing) were eliminated, but full-
name collisions still bit us — e.g., "Susan Collins" the Boston Fed
President matched the Senator from Maine because both have that exact
full name. Catching this cleanly with regex would require a hardcoded
disambiguation blocklist that grows with every collision found in the
wild.

This module hands disambiguation to Claude. The LLM reads the article
context and decides which (if any) catalog entry is actually being
referenced. Maintenance-free as the catalog grows.

How it stays cheap
------------------
Anthropic prompt caching: the catalog block (~7K tokens, rarely changes)
is marked with `cache_control: ephemeral`. Within the 5-minute TTL,
subsequent calls pay 10% of normal input price for the cached portion.

Cost shape:
- First call in a 5-min window:  ~7K input  + ~300 article + ~200 output
- Subsequent calls (same window): ~700 cached + ~300 article + ~200 output

At ~10 articles per 30-min refresh cycle (typical), the catalog cache
is hit once and reused 9 times → ~$0.001/article amortized.
At ~100 new articles/day → ~$3-5/month. Fall back to regex on any
LLM error so chips never disappear due to API blips.

Contract
--------
Same return shape as services/entity_linker.link_text, so callers can
swap in either implementation transparently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TypedDict

import anthropic

from app.config import settings
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.entity_linker_llm")

MODEL = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS = 500
# Per-article timeout — the LLM is fast (Haiku, small output) but if it
# stalls we fall back to regex rather than block the pipeline.
LLM_TIMEOUT_SECONDS = 8.0


class EntityLink(TypedDict):
    type: str
    canonical_id: str
    surface_form: str


class CatalogEntry(TypedDict):
    """One curated entity. Same shape build_catalog produces in the
    regex linker — kept compatible so the existing build helpers can
    feed both code paths."""

    type: str           # "outlet" | "politician" | "org" | "bill"
    canonical_id: str
    primary_name: str
    # Aliases are unused by the LLM linker (it does fuzzy matching on
    # primary_name + context), but kept in the type so the same catalog
    # row works for both paths.
    aliases: list[str]


# ── Prompt construction ────────────────────────────────────────────


_VALID_TYPES = frozenset({"outlet", "politician", "org", "bill"})


def _format_catalog_block(catalog: list[CatalogEntry]) -> str:
    """Render the catalog as a compact pipe-delimited table.

    Format intentionally minimizes tokens while preserving the
    type/canonical_id/name signal Claude needs.
    """
    by_type: dict[str, list[CatalogEntry]] = {}
    for row in catalog:
        by_type.setdefault(row["type"], []).append(row)

    lines: list[str] = []
    type_headings = {
        "politician": "POLITICIANS (sitting U.S. Congress members)",
        "org": "ORGANIZATIONS (think tanks, advocacy, PACs)",
        "bill": "BILLS",
        "outlet": "OUTLETS (news organizations)",
    }
    for type_key in ("politician", "org", "bill", "outlet"):
        rows = by_type.get(type_key, [])
        if not rows:
            continue
        lines.append("")
        lines.append(type_headings[type_key])
        for row in rows:
            lines.append(f"  {row['canonical_id']} | {row['primary_name']}")
    return "\n".join(lines).strip()


SYSTEM_INSTRUCTIONS = """You tag news articles with mentions of curated entities. \
Given an article and a roster, return JSON listing only entities the article \
specifically refers to.

ROSTER (only tag canonical_ids from this list — never invent a new id):

{catalog}

RULES:

1. Only tag entities present in the roster above. Never tag a person, org, \
or bill that's not listed.

2. Tag a politician only when the article clearly refers to THIS specific \
person. Names overlap in public life — for example, "Susan Collins" can \
refer to the Senator from Maine OR the Boston Fed President. Use article \
context (titles, organizations, locations, roles) to decide which person \
is meant. If unclear, omit the tag.

3. Tag an outlet only when its name appears in the article copy AND \
refers to that outlet's reporting (e.g., "according to Reuters"). Don't \
tag the article's own source — that's surfaced separately.

4. surface_form must be the exact substring as it appears in the article \
(preserve original casing).

5. Output JSON only — no prose, no markdown fences. Empty array if no \
roster entities are mentioned.

Schema:
[{{"type": "politician", "canonical_id": "S000148", "surface_form": "Chuck Schumer"}}, ...]"""


def _build_system_prompt(catalog: list[CatalogEntry]) -> str:
    return SYSTEM_INSTRUCTIONS.format(catalog=_format_catalog_block(catalog))


def _build_user_prompt(title: str, summary: str) -> str:
    title = (title or "").strip() or "(untitled)"
    summary = (summary or "").strip() or "(no summary)"
    return (
        f"Article title: {title}\n\n"
        f"Article summary: {summary}\n\n"
        "Return the JSON array now."
    )


# ── Response parsing ───────────────────────────────────────────────


def _extract_json_array(text: str) -> list[dict] | None:
    """Find the first JSON array in the LLM output. Tolerates leading/
    trailing prose and ```json fences."""
    text = text.strip()
    # Strip code fences if present.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Greedy-bracket fallback.
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _parse_response(
    text: str,
    valid_canonicals: dict[str, set[str]],
) -> list[EntityLink]:
    """Validate parsed entries against the catalog. Drops anything the
    model hallucinated (wrong type, unknown canonical_id, missing
    surface_form)."""
    parsed = _extract_json_array(text)
    if parsed is None:
        return []

    out: list[EntityLink] = []
    seen: set[tuple[str, str]] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        cid = entry.get("canonical_id")
        surface = entry.get("surface_form")
        if not isinstance(etype, str) or etype not in _VALID_TYPES:
            continue
        if not isinstance(cid, str) or not cid.strip():
            continue
        if not isinstance(surface, str) or not surface.strip():
            continue
        cid = cid.strip()
        # Reject hallucinations: the model must pick from the actual roster.
        if cid not in valid_canonicals.get(etype, set()):
            continue
        ref = (etype, cid)
        if ref in seen:
            continue
        seen.add(ref)
        out.append({
            "type": etype,
            "canonical_id": cid,
            "surface_form": surface.strip(),
        })

    out.sort(key=lambda e: (e["type"], e["canonical_id"]))
    return out


# ── Top-level link function ────────────────────────────────────────


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _index_catalog(catalog: list[CatalogEntry]) -> dict[str, set[str]]:
    """{type → set(canonical_id)} for fast hallucination rejection."""
    out: dict[str, set[str]] = {}
    for row in catalog:
        out.setdefault(row["type"], set()).add(row["canonical_id"])
    return out


async def link_text_llm(
    title: str,
    summary: str,
    catalog: list[CatalogEntry],
    *,
    client: anthropic.AsyncAnthropic | None = None,
) -> list[EntityLink]:
    """Single-article entity linking via Claude.

    Returns [] on any failure path (API error, parse error, timeout).
    The caller is responsible for falling back to the regex linker if
    that matters — usually it does, since '[]' is a valid output for
    'no entities mentioned'.
    """
    if not (title or summary) or not catalog:
        return []

    client = client or _client()
    system_prompt = _build_system_prompt(catalog)
    user_prompt = _build_user_prompt(title, summary)
    valid = _index_catalog(catalog)

    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                # Cache the catalog block so we only pay full price for it
                # the first call in each 5-min window.
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[{"role": "user", "content": user_prompt}],
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("entity_linker_llm: %.1fs timeout", LLM_TIMEOUT_SECONDS)
        return []
    except Exception as e:  # noqa: BLE001 — log + degrade gracefully
        logger.warning("entity_linker_llm: API error: %s", e)
        return []

    log_usage("entity_linker_llm.link_text", response, model=MODEL)

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_response(text, valid)


async def link_articles_llm(
    articles: list[dict],
    catalog: list[CatalogEntry],
    *,
    concurrency: int = 4,
) -> dict[str, list[EntityLink]]:
    """Batch wrapper: run link_text_llm over `articles`, keyed by
    `source_url`. Articles without a source_url are skipped.

    Concurrency-limited so we don't burst Claude in a single tick.
    """
    out: dict[str, list[EntityLink]] = {}
    if not articles or not catalog:
        return {a.get("source_url", ""): [] for a in articles if a.get("source_url")}

    client = _client()
    sem = asyncio.Semaphore(concurrency)

    async def _link_one(article: dict) -> tuple[str, list[EntityLink]]:
        async with sem:
            url = article.get("source_url") or ""
            if not url:
                return "", []
            links = await link_text_llm(
                article.get("title") or "",
                article.get("summary") or "",
                catalog,
                client=client,
            )
            return url, links

    results = await asyncio.gather(*(_link_one(a) for a in articles))
    for url, links in results:
        if url:
            out[url] = links
    return out
