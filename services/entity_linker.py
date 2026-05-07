"""Entity linker — civic-literacy MVP Phase 3.G.

Resolves surface-form mentions in article text (title + summary) to
canonical IDs in the four curated profile tables:

  outlet_profiles      → slug
  politician_profiles  → bioguide_id
  org_profiles         → slug
  bill_profiles        → bill_id

The result list is stored on `articles.entity_links` (denormalized
JSONB) for the frontend to render via InlineGlossaryTooltip.

Implementation: deterministic regex word-boundary matching against a
search dictionary built from the canonical names + a small set of
high-precision aliases. **No LLM call** — fast, free, deterministic,
auditable. Trade-off: misses common surface-form variants (e.g.,
"Sen. Schumer" when the canonical name is "Chuck Schumer"); a future
3.G.2 can layer LLM-based extraction on top if recall matters.

Aliases applied:

* Politicians: full canonical name only. We deliberately do NOT alias
  to last-name-only — even with uniqueness + length checks, common-noun
  surnames (Cloud, Self, Case, Strong, Banks, Hill, Young, Downing, ...)
  generate constant false positives in news copy ("cloud computing",
  "the case involves", "Cloud AI", "China Asks Banks to Pause", etc.).
  Recall trade-off accepted: a "Schumer said" reference loses its link,
  but journalism typically introduces politicians by full name on first
  mention — which we still catch. Better to under-link than mislink for
  a portfolio site whose credibility hinges on signal-to-noise.
* Orgs: full canonical name only. Initials/abbreviations are too
  ambiguous without per-org curation.
* Bills: short_title (when present) + bill_id ("hr-5376-117"). The
  bill_id form is rare in journalism but consistent.
* Outlets: full canonical name only.

Stop-word filter: surface forms shorter than 4 characters or matching
common-English-word strings ("the", "and", "for") are dropped from the
search dictionary at build time, so a politician named "and" couldn't
hijack the linker even if curated by mistake.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Iterable, TypedDict

logger = logging.getLogger("sift-api.entity_linker")


class EntityLink(TypedDict):
    """One resolved entity reference in an article."""

    type: str           # "outlet" | "politician" | "org" | "bill"
    canonical_id: str   # outlet_profiles.slug | bioguide_id | org_profiles.slug | bill_id
    surface_form: str   # the matched substring as it appeared


# Common short words that must never become standalone search keys, no
# matter what gets curated. (Defensive — a curator typo'ing "And" as a
# politician's nickname shouldn't take down the linker.)
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "been", "were",
    "have", "has", "his", "her", "its", "all", "but", "are", "any",
    "new", "now", "one", "two", "off", "out", "you", "our", "who", "how",
})

# Minimum length for a search-key to be eligible. Below this, false-positive
# rate dominates real matches.
_MIN_KEY_LENGTH = 4


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace. Used for both keys and inputs."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _word_pattern(text: str) -> re.Pattern[str]:
    """Compile a case-insensitive whole-word regex for the surface form.

    Word boundaries are `\\b` for alphanumeric edges; for surface forms
    that contain hyphens or periods (e.g., "H.R. 5376"), we anchor on
    non-word boundaries instead. Keep it simple: just escape and bracket
    with `\\b` at start/end, accept that some weird surface forms (like
    a leading punctuation token) won't match — they're rare.
    """
    escaped = re.escape(text)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)


# ── Catalog shape ──────────────────────────────────────────────────────


class CatalogRow(TypedDict):
    """One curated entity, agnostic to which profile table it came from."""

    type: str           # "outlet" | "politician" | "org" | "bill"
    canonical_id: str
    primary_name: str
    aliases: list[str]  # additional acceptable surface forms (last name, short_title, bill_id)


def build_search_dict(
    rows: Iterable[CatalogRow],
) -> dict[str, tuple[str, str]]:
    """Build a {normalized_surface_form: (type, canonical_id)} lookup.

    Conflict resolution: if the same surface form maps to multiple
    entities, drop it entirely. Better to miss a match than to point
    "Apple" at the wrong Apple.

    Stop-words and short keys are filtered.
    """
    # First pass: collect every candidate key. Track conflicts.
    candidates: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        keys = [row["primary_name"], *row.get("aliases", [])]
        for key in keys:
            normalized = _normalize(key)
            if not normalized:
                continue
            if normalized in _STOPWORDS:
                continue
            if len(normalized) < _MIN_KEY_LENGTH:
                continue
            candidates.setdefault(normalized, []).append(
                (row["type"], row["canonical_id"]),
            )

    # Second pass: drop ambiguous keys.
    out: dict[str, tuple[str, str]] = {}
    dropped = 0
    for key, refs in candidates.items():
        # Same canonical_id appearing twice (primary + alias both lower
        # to the same string) is fine — keep the first.
        unique_refs = list({(t, cid) for t, cid in refs})
        if len(unique_refs) == 1:
            out[key] = unique_refs[0]
        else:
            dropped += 1
            logger.debug(
                "entity_linker: dropping ambiguous key %r (refs: %r)",
                key, unique_refs,
            )
    if dropped:
        logger.info(
            "entity_linker: dropped %d ambiguous search keys at build time",
            dropped,
        )
    return out


def politician_aliases(name: str, lastname_freq: Counter[str]) -> list[str]:
    """Build a list of acceptable surface forms for a politician.

    Returns an empty list — only the full canonical name is searchable.

    Last-name-only aliases were originally added when uniqueness +
    `_MIN_KEY_LENGTH` were assumed sufficient guards. They aren't.
    Common-noun surnames in the curated roster (Cloud, Self, Case,
    Strong, Banks, Hill, Young, Downing, Field, Stone, Reed, Forest, ...)
    constantly false-match in news copy and even more so in title-cased
    headlines ("Cloud AI", "China Asks Banks to Pause"), where
    case-sensitivity alone wouldn't help.

    Kept as a function (rather than inlined) so a future Phase 3.G.2
    can add back smarter aliasing — e.g., LLM-confirmed mention type,
    or a curated common-noun blocklist — without changing the call site
    in `build_catalog`.

    `lastname_freq` is now unused but kept in the signature so callers
    don't break (and so we still need `Counter` if the policy reverses).
    """
    del name, lastname_freq  # explicit no-op until Phase 3.G.2
    return []


def build_catalog(
    outlets: list[dict],
    politicians: list[dict],
    orgs: list[dict],
    bills: list[dict],
) -> list[CatalogRow]:
    """Assemble the four input lists into a uniform catalog.

    Inputs are dicts straight off asyncpg — see entity_linker_node for
    the actual queries. We normalize them here so the matcher only deals
    with one shape.
    """
    rows: list[CatalogRow] = []

    for o in outlets:
        name = (o.get("name") or "").strip()
        slug = (o.get("slug") or "").strip().lower()
        if not name or not slug:
            continue
        rows.append(CatalogRow(
            type="outlet", canonical_id=slug, primary_name=name, aliases=[],
        ))

    # Politicians: compute lastname frequency so unambiguous lastnames
    # become aliases.
    lastname_freq: Counter[str] = Counter()
    for p in politicians:
        name = (p.get("name") or "").strip()
        parts = name.split()
        if len(parts) >= 2:
            lastname_freq[parts[-1].lower()] += 1
    for p in politicians:
        name = (p.get("name") or "").strip()
        bid = (p.get("bioguide_id") or "").strip()
        if not name or not bid:
            continue
        rows.append(CatalogRow(
            type="politician",
            canonical_id=bid,
            primary_name=name,
            aliases=politician_aliases(name, lastname_freq),
        ))

    for o in orgs:
        name = (o.get("name") or "").strip()
        slug = (o.get("slug") or "").strip().lower()
        if not name or not slug:
            continue
        rows.append(CatalogRow(
            type="org", canonical_id=slug, primary_name=name, aliases=[],
        ))

    for b in bills:
        title = (b.get("short_title") or b.get("title") or "").strip()
        bill_id = (b.get("bill_id") or "").strip().lower()
        if not title or not bill_id:
            continue
        # Aliases:
        #   - The canonical slug form ("hr-5376-117") — rare in journalism
        #     but consistent.
        #   - A year-stripped variant for short titles like "Inflation
        #     Reduction Act of 2022", which journalism almost always
        #     shortens to "Inflation Reduction Act". Strips trailing
        #     " of YYYY" only — keeps high precision.
        aliases = [bill_id]
        year_stripped = re.sub(r"\s+of\s+\d{4}\s*$", "", title, flags=re.IGNORECASE)
        if year_stripped != title and len(year_stripped) >= _MIN_KEY_LENGTH:
            aliases.append(year_stripped)
        rows.append(CatalogRow(
            type="bill",
            canonical_id=bill_id,
            primary_name=title,
            aliases=aliases,
        ))

    return rows


def link_text(
    text: str,
    search_dict: dict[str, tuple[str, str]],
) -> list[EntityLink]:
    """Run every search key against `text`, return one EntityLink per
    distinct canonical_id matched. Multiple distinct surface forms for
    the same entity collapse to a single link (uses the first match's
    surface_form for display).
    """
    if not text or not search_dict:
        return []

    seen: dict[tuple[str, str], EntityLink] = {}
    for key, (etype, cid) in search_dict.items():
        # Compile per-call to avoid stale-cache complications; the
        # catalog is small (~700 rows) and pattern compilation is cheap.
        pattern = _word_pattern(key)
        match = pattern.search(text)
        if match is None:
            continue
        ref = (etype, cid)
        if ref in seen:
            continue
        seen[ref] = EntityLink(
            type=etype,
            canonical_id=cid,
            surface_form=match.group(0),  # preserves original casing
        )

    # Stable sort: by entity type then by canonical_id, so the JSONB
    # array reads consistently across runs.
    return sorted(
        seen.values(),
        key=lambda e: (e["type"], e["canonical_id"]),
    )


async def link_articles(articles: list[dict]) -> dict[str, list[EntityLink]]:
    """Async wrapper that loads the catalog from Postgres and resolves
    entity mentions in each article.

    Strategy (Phase 3.G.2): primary path is the LLM linker
    (services.entity_linker_llm), which handles full-name collisions
    (Susan Collins the Senator vs the Boston Fed President) without a
    hardcoded blocklist. Falls back to the regex `link_text` matcher on
    any LLM error so chips never disappear due to API blips.

    Input shape: list of {source_url, title, summary, ...}.
    Output: {source_url: [EntityLink, ...]}.

    Tolerant of missing tables (returns empty links per article) so the
    pipeline doesn't break on pre-Phase-3.A-merge prod.
    """
    from app.db import get_pool

    if not articles:
        return {}

    try:
        pool = await get_pool()
        outlets = [dict(r) for r in await pool.fetch("SELECT slug, name FROM outlet_profiles")]
        politicians = [
            dict(r) for r in await pool.fetch(
                "SELECT bioguide_id, name FROM politician_profiles"
            )
        ]
        orgs = [dict(r) for r in await pool.fetch("SELECT slug, name FROM org_profiles")]
        bills = [
            dict(r) for r in await pool.fetch(
                "SELECT bill_id, title, short_title FROM bill_profiles"
            )
        ]
    except Exception as e:
        msg = str(e)
        if "does not exist" in msg:
            logger.info(
                "entity_linker: profile tables missing — returning empty links "
                "for all %d articles (graceful degradation)",
                len(articles),
            )
            return {a.get("source_url", ""): [] for a in articles if a.get("source_url")}
        raise

    catalog = build_catalog(outlets, politicians, orgs, bills)
    logger.info(
        "entity_linker: catalog loaded — %d outlets, %d politicians, %d orgs, %d bills",
        len(outlets), len(politicians), len(orgs), len(bills),
    )

    # Primary: LLM linker. Falls back to regex per-article on any error.
    out: dict[str, list[EntityLink]] = {}
    try:
        from services.entity_linker_llm import link_articles_llm
        out = await link_articles_llm(articles, catalog)  # type: ignore[arg-type]
        logger.info(
            "entity_linker: LLM path resolved %d articles",
            sum(1 for v in out.values() if v is not None),
        )
    except Exception as e:  # noqa: BLE001 — degrade rather than block the pipeline
        logger.warning(
            "entity_linker: LLM path failed (%s) — falling back to regex for all %d",
            e, len(articles),
        )

    # For any article the LLM path didn't resolve (missing url, error, or
    # silently-empty), fall back to the regex matcher.
    search_dict = build_search_dict(catalog)
    fallback_used = 0
    total_links = 0
    for article in articles:
        url = article.get("source_url")
        if not url:
            continue
        if url not in out:
            title = article.get("title") or ""
            summary = article.get("summary") or ""
            text = f"{title}\n{summary}"
            out[url] = link_text(text, search_dict)
            fallback_used += 1
        total_links += len(out[url])
    if fallback_used:
        logger.info("entity_linker: regex-fallback used for %d/%d articles",
                    fallback_used, len(articles))

    logger.info(
        "entity_linker: resolved %d links across %d articles (avg %.1f/article)",
        total_links, len(out), total_links / max(len(out), 1),
    )
    return out
