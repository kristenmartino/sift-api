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
high-precision aliases, with **longest-match-wins** overlap resolution
(see `link_text`) so a short key nested inside a longer name doesn't
also fire. `link_text` itself makes **no LLM call** — fast, free,
deterministic, auditable. Trade-off: it misses common surface-form
variants (e.g., "Sen. Schumer" when the canonical name is "Chuck
Schumer").

Phase 3.G.2 layered the LLM linker on top for exactly that reason, so
`link_articles` is no longer regex-only: the regex matcher is now the
pre-gate and the per-article fallback, and disambiguation happens in
`services.entity_linker_llm`. See `link_articles` for the routing.

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

Common-English *names*: neither guard above helps when the canonical name
is itself an ordinary phrase — "The Nation", "Foreign Policy", "Nature".
`_REGEX_INELIGIBLE_NAMES` is the curated blocklist for those; see its
comment for the measured false-positive rates. It withholds them from the
regex dictionary only — they remain in the catalog the LLM linker reads.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Iterable, NotRequired, TypedDict

from app.config import settings

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

# Canonical names that are also ordinary English, and must therefore never
# become regex keys. The plan called this `regex_eligible`; it is expressed
# as the deny-half because ~10 of 200 catalog names need it and the rest are
# unambiguous proper nouns.
#
# `_STOPWORDS` and `_MIN_KEY_LENGTH` do not cover this case: the former holds
# only single common words, and a 4-char floor says nothing about a
# multi-word phrase like "the nation" or "foreign policy". Measured in prod
# 2026-08-05, a regex-mode `backfill_entity_links.py --include-empty` run put
# 5,838 bad chips on 3,872 articles from five of these names alone.
#
# **This blocks the regex dictionary only.** The rows stay in the catalog, so
# `entity_linker_llm` still sees them and can link them from context — which
# is the whole reason the LLM path exists and why a blocklist is the right
# shape here. The cost is gate recall: an article whose *only* catalog name is
# a blocked one now matches nothing, so the pre-gate answers [] without asking
# the LLM. That is the trade this file already makes explicit elsewhere —
# "better to under-link than mislink" (see politician_aliases).
#
# Rates below are the share of corpus matches that are the English phrase
# rather than the outlet, measured over random samples of prod `articles`
# (n=20-30 each) on 2026-08-05 via a one-off audit. Sample RANDOMLY, not by
# `published_date DESC` — recency-ordered sampling made "The Athletic" look
# 67% false (August college-sports copy) when it is really ~12%.
#
#   the nation      ~100%  "the nation's fuel", "Face the Nation"
#   foreign affairs  100%  "House Foreign Affairs Committee" — 0 real in n=20
#   reason           ~95%  "the top cited reason for layoffs"
#   nature           ~93%  only ~51 of 746 linked articles were the journal
#   the free press   ~86%  "a takeover of the free press" (n=7 total)
#   foreign policy   ~85%  "U.S. foreign policy priorities"
#   the times        ~75%  "Sign of the Times"; and most of the remainder are
#                          the New York Times, i.e. the wrong canonical_id
#   slate            ~70%  "a robust slate of returning shows"
#   the verge         47%  "on the verge of" — 14 of 30 sampled
#   the atlantic      40%  "the Atlantic Ocean", "the Atlantic Division"
#   stat             100%  of third-party copy — see below
#
# `stat` was added 2026-08-06, and the first audit had cleared it at "~0%".
# **Count the share separately for the outlet's OWN articles and for everybody
# else's.** Of 873 stored `stat-news` chips, 762 sat on STAT News's own copy
# and 111 on third-party copy, so a pooled rate reads 12.7% — respectable, and
# wrong. Split, it is 0% on STAT's own articles and **111 of 111 on everyone
# else's**: "Red Sox's Most Absurd Stat", "this Walker Kessler stat", "add to
# your cart stat". A self-reference is the one match that proves nothing, since
# the outlet is already named in `source_name`; the corpus carries **zero**
# third-party mentions of STAT in any casing. That is the shape to watch for on
# any outlet that publishes into its own corpus, and it is invisible to a
# pooled sample no matter how randomly it is drawn.
#
# Deliberately NOT blocked, having been measured rather than assumed:
# the athletic (~12% — "the athletic department"), the hill (~15%), variety
# (~6%), the guardian (~3%), wired / forbes / the economist / national review /
# the dispatch (~0%). "the federalist" survives on its own because
# longest-match-wins hands "the Federalist Society" to the org row. Every
# `org_profiles.name` and `bill_profiles.short_title` was audited too: all are
# distinctive proper nouns, none collide.
#
# **Those four rates are under review and are probably too low** (sift-api#151,
# measured 2026-08-06). Screening every stored chip for an all-lowercase
# `surface_form` — a rendering a proper noun essentially never takes in edited
# copy — flags `variety` 42 of 67 chips, `the athletic` 64 of 123, `wired` 19
# of 79 and `the hill` 3 of 84, and every one sampled was the common noun ("a
# variety of", "the athletic department", "electronically wired to", "the hill
# to die on"). They are NOT blocked here because, unlike `stat`, each also has
# real correctly-cased third-party mentions that a blocklist would destroy;
# the fix they need is a casing rule, not a blocklist entry. See #151.
#
# Like `_STOPWORDS`, this applies to curated `entity_aliases` rows as well —
# a blocked name cannot be smuggled back in through the alias table. That is
# also the escape hatch: to restore recall for one of these, curate a
# *narrower* alias ("A Nature study") rather than reopening the bare name.
#
# Pick that narrower form by MEASURING it, not by inventing one that reads
# well. "the journal Nature" stood here until 2026-08-06 and occurs **zero**
# times in the corpus. The obvious replacement is worse: "published in
# Nature" appears 88 times third-party, but 62 of those continue into a
# *different* journal ("published in Nature Communications", "…Medicine",
# "…Physics"), and a whole-phrase key matches on the word boundary after
# "Nature", so it would chip all 88 — 70% of them wrong. The whole-phrase
# matcher cannot express "not followed by a sub-brand", so those 26 genuine
# bare mentions stay with the LLM path by design.
_REGEX_INELIGIBLE_NAMES: frozenset[str] = frozenset({
    "the nation",
    "nature",
    "foreign policy",
    "foreign affairs",
    "reason",
    "slate",
    "the verge",
    "the atlantic",
    "the times",
    "the free press",
    # Blocks the bare word only. The curated "stat news" alias is unaffected
    # (it is a different key) and is what keeps the outlet linkable.
    "stat",
})

# The same floor for keys that came from the curated entity_aliases table.
# The 4-char floor is a blunt proxy for "this key was derived, so nobody
# vouched for it" — and it silently made several of the most-mentioned outlets
# in the corpus unlinkable: CNN (397 articles), NPR (241), Vox (77), plus the
# bare "BBC" that copy uses instead of "BBC News" (1,068). A curated row is a
# human's explicit, `notes`-justified claim about one surface form, so the
# proxy does not apply. Kept at 2 rather than 1 because a single character
# cannot be an unambiguous entity reference, curated or not; _STOPWORDS still
# applies to both paths.
_MIN_CURATED_KEY_LENGTH = 2


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace. Used for both keys and inputs."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _key_is_cased(key: str) -> bool:
    """True when a search key must be matched case-sensitively.

    `build_search_dict` lowercases every key it derives, so uppercase can
    only survive into the dict because a curator asked for it via
    `entity_aliases.match_case` (migration 017). That makes the key's own
    casing a sufficient signal, and lets `search_dict` stay the plain
    `{key: (type, canonical_id)}` mapping that `link_text`, the backfill
    script and `eval_linker_gate` all read positionally.

    The alternative — widening the value to a 3-tuple — would have been
    more explicit at the cost of touching every consumer and ~15 test
    assertions for one boolean that only two rows will ever set.
    """
    return key != key.lower()


def _word_pattern(text: str, *, match_case: bool = False) -> re.Pattern[str]:
    """Compile a whole-word regex for the surface form.

    Case-insensitive by default. `match_case=True` compiles without
    `IGNORECASE`, which is what makes an acronym whose lowercase form is
    an ordinary word usable as a key at all: `ICE` is the agency in 1,725
    corpus articles, while `ice` is ice cream, sea ice and hockey in 672
    more. See migration 017.

    Word boundaries are `\\b` for alphanumeric edges; for surface forms
    that contain hyphens or periods (e.g., "H.R. 5376"), we anchor on
    non-word boundaries instead. Keep it simple: just escape and bracket
    with `\\b` at start/end, accept that some weird surface forms (like
    a leading punctuation token) won't match — they're rare.
    """
    escaped = re.escape(text)
    return re.compile(rf"\b{escaped}\b", 0 if match_case else re.IGNORECASE)


# ── Catalog shape ──────────────────────────────────────────────────────


class CatalogRow(TypedDict):
    """One curated entity, agnostic to which profile table it came from."""

    type: str           # "outlet" | "politician" | "org" | "bill"
    canonical_id: str
    primary_name: str
    aliases: list[str]  # additional acceptable surface forms (last name, short_title, bill_id)
    # The subset of `aliases` that came from the hand-curated entity_aliases
    # table. A marker, not a second store — the values stay in `aliases` so
    # every existing consumer (notably _format_catalog_block in the LLM
    # linker) keeps seeing them. Its only job is to tell build_search_dict
    # which keys a human vouched for, so the length floor can be relaxed for
    # those and only those.
    curated: NotRequired[list[str]]
    # The subset of `aliases` carrying entity_aliases.match_case (migration
    # 017), in the exact casing to match on. Same marker-not-a-store shape as
    # `curated` above: the value also stays in `aliases`, so the LLM linker's
    # roster is unchanged. build_search_dict keeps these keys cased instead of
    # lowercasing them, which is how `link_text` knows to drop IGNORECASE.
    cased: NotRequired[list[str]]


def build_search_dict(
    rows: Iterable[CatalogRow],
) -> dict[str, tuple[str, str]]:
    """Build a {normalized_surface_form: (type, canonical_id)} lookup.

    Conflict resolution: if the same surface form maps to multiple
    entities, drop it entirely. Better to miss a match than to point
    "Apple" at the wrong Apple.

    Stop-words, common-English catalog names (`_REGEX_INELIGIBLE_NAMES`)
    and short keys are filtered.
    """
    # First pass: collect every candidate key. Track conflicts.
    #
    # Every gate below tests the *normalized* form even for a case-sensitive
    # key, so casing cannot be used to smuggle a key past them: aliasing
    # "Nature" onto the journal is rejected by _REGEX_INELIGIBLE_NAMES exactly
    # as "nature" is. Only the key finally emitted keeps its casing.
    # `emitted` maps normalized -> the string to key the output dict by.
    candidates: dict[str, list[tuple[str, str]]] = {}
    emitted: dict[str, str] = {}
    ineligible = 0
    for row in rows:
        curated = {_normalize(a) for a in row.get("curated", [])}
        cased = {_normalize(a): a for a in row.get("cased", [])}
        keys = [row["primary_name"], *row.get("aliases", [])]
        for key in keys:
            normalized = _normalize(key)
            if not normalized:
                continue
            if normalized in _STOPWORDS:
                continue
            # Ordinary English that happens to be a catalog name. Checked
            # before the curated-alias floor below so the alias table cannot
            # reintroduce one. The row itself is untouched — the LLM linker
            # reads `rows`, not this dict, and still gets to link it.
            if normalized in _REGEX_INELIGIBLE_NAMES:
                ineligible += 1
                logger.debug(
                    "entity_linker: %r is regex-ineligible (common English) — "
                    "LLM path only", normalized,
                )
                continue
            # Curated keys get the lower floor: the 4-char rule is a proxy for
            # "derived, so unvouched-for", and a hand-checked row is exactly
            # the case it was never meant to catch. See _MIN_CURATED_KEY_LENGTH.
            floor = (
                _MIN_CURATED_KEY_LENGTH if normalized in curated
                else _MIN_KEY_LENGTH
            )
            if len(normalized) < floor:
                continue
            candidates.setdefault(normalized, []).append(
                (row["type"], row["canonical_id"]),
            )
            # Keyed by the normalized form so a case-sensitive alias and an
            # ordinary key that lower to the same string still collide in the
            # conflict pass below — "ICE" for the agency and "ice" for some
            # other entity must drop each other, not coexist.
            if normalized in cased:
                emitted[normalized] = cased[normalized]
            else:
                emitted.setdefault(normalized, normalized)

    # Second pass: drop ambiguous keys.
    out: dict[str, tuple[str, str]] = {}
    dropped = 0
    for key, refs in candidates.items():
        # Same canonical_id appearing twice (primary + alias both lower
        # to the same string) is fine — keep the first.
        unique_refs = list({(t, cid) for t, cid in refs})
        if len(unique_refs) == 1:
            out[emitted.get(key, key)] = unique_refs[0]
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
    if ineligible:
        logger.info(
            "entity_linker: withheld %d regex-ineligible key(s) — still "
            "linkable via the LLM path", ineligible,
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


def nickname_variants(name: str) -> list[str]:
    """Expand a "Given (Nickname) Surname" roster entry into the forms prose uses.

    The bioguide roster stores congressional nicknames inline — "Charles
    (Chuck) Edwards" — and that literal string never appears in journalism, so
    the canonical name is unmatchable. Measured 2026-08-05 via
    scripts/eval_linker_gate.py, E000246 was the third-largest single source of
    linker misses (11 of ~110) for exactly this reason. Five of 638 politicians
    carry the pattern.

    Returns both readings: "Charles Edwards" and "Chuck Edwards".

    THIS IS NOT THE ALIASING #40 REMOVED. That was bare surnames, where
    "Banks" / "Hill" / "Moody" collide with ordinary nouns and ratings
    agencies. Every variant here keeps a given name AND a surname — the
    two-token floor below enforces it — so none of that risk applies. See
    politician_aliases() for the policy this deliberately does not reopen.

    Politician-only by design. The same syntax means something else on the
    other rosters: outlet_profiles carries "Science (AAAS)", where the
    parenthetical is an acronym, and the stripped reading "Science" would
    match a large fraction of the corpus.
    """
    if "(" not in name:
        return []

    readings = (
        # "Charles (Chuck) Edwards" -> "Charles Edwards"
        re.sub(r"\s*\([^)]*\)\s*", " ", name),
        # "Charles (Chuck) Edwards" -> "Chuck Edwards". The nickname REPLACES
        # the given name it annotates, so the optional \S+ swallows it;
        # without that this yields "Charles Chuck Edwards", which is not a
        # name anyone writes. The group stays optional so a leading
        # "(Chuck) Edwards" still reads as "Chuck Edwards".
        re.sub(r"(?:\S+\s+)?\(([^)]*)\)", r"\1", name),
    )
    out: list[str] = []
    for reading in readings:
        collapsed = re.sub(r"\s+", " ", reading).strip()
        # Two tokens minimum: a single bare token IS the #40 hazard, and it is
        # what a malformed entry like "(Chuck)" would otherwise produce.
        if collapsed and collapsed != name and len(collapsed.split()) >= 2:
            out.append(collapsed)
    return list(dict.fromkeys(out))


def build_catalog(
    outlets: list[dict],
    politicians: list[dict],
    orgs: list[dict],
    bills: list[dict],
    aliases: list[dict] | None = None,
) -> list[CatalogRow]:
    """Assemble the four input lists into a uniform catalog.

    Inputs are dicts straight off asyncpg — see entity_linker_node for
    the actual queries. We normalize them here so the matcher only deals
    with one shape.

    `aliases` is the curated entity_aliases table (migration 014): rows of
    {alias, entity_type, canonical_id}. Unlike the derived last-name aliases
    removed in #40, every one of these is hand-checked, so they are safe to
    put in front of the regex matcher. Optional so existing callers and any
    pre-014 database keep working.
    """
    rows: list[CatalogRow] = []

    # {(type, canonical_id): [alias, ...]} — attached to each row below.
    # `extra_cased` is the match_case subset (migration 017), tracked
    # separately so build_search_dict can keep those keys cased.
    extra: dict[tuple[str, str], list[str]] = {}
    extra_cased: dict[tuple[str, str], list[str]] = {}
    for a in aliases or []:
        surface = (a.get("alias") or "").strip()
        etype = (a.get("entity_type") or "").strip()
        cid = (a.get("canonical_id") or "").strip()
        if surface and etype and cid:
            extra.setdefault((etype, cid), []).append(surface)
            # `.get` with a default so a pre-017 database, whose rows have no
            # such column, reads as all-false rather than raising.
            if a.get("match_case"):
                extra_cased.setdefault((etype, cid), []).append(surface)

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
            aliases=[
                *politician_aliases(name, lastname_freq),
                *nickname_variants(name),
            ],
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
        # Named bill_aliases, not aliases: the latter is now a parameter and
        # rebinding it here would silently drop the curated alias table.
        bill_aliases = [bill_id]
        year_stripped = re.sub(r"\s+of\s+\d{4}\s*$", "", title, flags=re.IGNORECASE)
        if year_stripped != title and len(year_stripped) >= _MIN_KEY_LENGTH:
            bill_aliases.append(year_stripped)
        rows.append(CatalogRow(
            type="bill",
            canonical_id=bill_id,
            primary_name=title,
            aliases=bill_aliases,
        ))

    # Merge the curated aliases onto whichever row they target. An alias
    # pointing at a canonical_id that no longer exists is dropped and logged —
    # a dangling alias means a dossier was deleted or a slug changed, which is
    # the D40 drift failure and should be visible, not silent.
    if extra:
        by_key = {(r["type"], r["canonical_id"]): r for r in rows}
        attached = 0
        for key, surfaces in extra.items():
            row = by_key.get(key)
            if row is None:
                logger.warning(
                    "entity_linker: %d alias(es) target missing %s %r — dropped",
                    len(surfaces), key[0], key[1],
                )
                continue
            row["aliases"] = [*row.get("aliases", []), *surfaces]
            # Same values, recorded a second time as provenance. `aliases` is
            # what every consumer reads; `curated` only tells build_search_dict
            # that a human vouched for these, which is what earns them the
            # lower length floor.
            row["curated"] = [*row.get("curated", []), *surfaces]
            cased_here = extra_cased.get(key)
            if cased_here:
                row["cased"] = [*row.get("cased", []), *cased_here]
            attached += len(surfaces)
        n_cased = sum(len(v) for v in extra_cased.values())
        logger.info(
            "entity_linker: attached %d curated aliases (%d case-sensitive)",
            attached, n_cased,
        )

    return rows


def link_text(
    text: str,
    search_dict: dict[str, tuple[str, str]],
) -> list[EntityLink]:
    """Run every search key against `text`, return one EntityLink per
    distinct canonical_id matched. Multiple distinct surface forms for
    the same entity collapse to a single link (uses the earliest
    surviving match's surface_form for display).

    **Longest-match-wins.** Keys are matched independently, so a short
    key nested inside a longer one fires on the same span: "The Library
    of Congress opened an exhibit" matches both `library of congress`
    and `congress`, which without resolution puts a wrong
    `united-states-congress` chip next to the correct one. When two
    matched spans overlap, only the longest survives; ties break by
    earlier start, then by key, so the result is deterministic.

    Resolution is per *occurrence*, not per key — a short key suppressed
    at one position still counts where it stands alone. "The Library of
    Congress said Congress had adjourned" yields both entities.
    """
    if not text or not search_dict:
        return []

    # Pass 1: every occurrence of every key, with its span. `finditer`
    # rather than `search` because a key losing its first occurrence to a
    # longer overlap may still own a later one.
    matches: list[tuple[int, int, str, str, str]] = []
    for key, (etype, cid) in search_dict.items():
        # Compile per-call to avoid stale-cache complications; the
        # catalog is small (~700 rows) and pattern compilation is cheap.
        pattern = _word_pattern(key, match_case=_key_is_cased(key))
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), key, etype, cid))
    if not matches:
        return []

    # Pass 2: greedy longest-first. Anything overlapping an already-kept
    # span is dropped — that is the whole of "longest wins", since the
    # longest candidate for any span is considered first.
    matches.sort(key=lambda m: (-(m[1] - m[0]), m[0], m[2]))
    kept: list[tuple[int, int, str, str, str]] = []
    for candidate in matches:
        start, end = candidate[0], candidate[1]
        if any(start < k_end and k_start < end for k_start, k_end, *_ in kept):
            continue
        kept.append(candidate)

    # Pass 3: collapse to one link per entity, keeping the earliest
    # surviving occurrence as the displayed surface form.
    kept.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    seen: dict[tuple[str, str], EntityLink] = {}
    for start, end, _key, etype, cid in kept:
        ref = (etype, cid)
        if ref in seen:
            continue
        seen[ref] = EntityLink(
            type=etype,
            canonical_id=cid,
            surface_form=text[start:end],  # preserves original casing
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
    any LLM error so chips never disappear due to API blips — both when
    the whole batch raises and, via `omit_failures=True`, when a single
    article's call fails. An LLM `[]` is a verdict, not a failure, and is
    kept as-is; only articles it never answered for fall back.

    Input shape: list of {source_url, title, summary, ...}.
    Output: {source_url: [EntityLink, ...]}.

    **Invariant**: every input article with a truthy `source_url` gets a
    key in the output. `store_node` indexes by source_url, so a missing
    key would silently drop that article's chips.

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
        # Curated aliases (migration 014), with the match_case flag (017).
        #
        # Three schema states, degrading one step at a time. The middle one is
        # the trap: on a pre-017 database the error reads "column
        # entity_aliases.match_case does not exist", which also matches the
        # table-absent test below — so a single try/except would silently drop
        # all 96 aliases rather than just the flag. Retry without the column
        # before concluding the table is gone.
        try:
            aliases = [
                dict(r) for r in await pool.fetch(
                    "SELECT alias, entity_type, canonical_id, match_case "
                    "FROM entity_aliases"
                )
            ]
        except Exception as alias_err:
            if "does not exist" not in str(alias_err):
                raise
            try:
                aliases = [
                    dict(r) for r in await pool.fetch(
                        "SELECT alias, entity_type, canonical_id FROM entity_aliases"
                    )
                ]
                logger.info(
                    "entity_linker: entity_aliases.match_case absent (pre-017) — "
                    "%d aliases loaded, all case-insensitive", len(aliases),
                )
            except Exception as table_err:
                if "does not exist" not in str(table_err):
                    raise
                logger.info(
                    "entity_linker: entity_aliases table absent — canonical names only"
                )
                aliases = []
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

    catalog = build_catalog(outlets, politicians, orgs, bills, aliases)
    logger.info(
        "entity_linker: catalog loaded — %d outlets, %d politicians, %d orgs, %d bills",
        len(outlets), len(politicians), len(orgs), len(bills),
    )

    search_dict = build_search_dict(catalog)

    # Regex pre-gate. The LLM linker costs one realtime call per article and
    # exists to *disambiguate* candidates ("Susan Collins" the Senator vs the
    # Boston Fed President), not to *discover* entities whose names never
    # appear in the text. So an article where the regex finds no catalog
    # surface form at all has nothing for the LLM to disambiguate, and we can
    # answer [] for free.
    #
    # Measured by scripts/eval_linker_gate.py over 12,690 articles / 7 days:
    # forwards 26% of articles and retains 98.11% of the links the ungated LLM
    # path produced. See entity_linker_regex_gate_enabled in app/config.py for
    # what the other 1.9% is and why it is acceptable.
    gated_out: set[str] = set()
    to_link = articles
    if settings.entity_linker_regex_gate_enabled:
        to_link = []
        for article in articles:
            url = article.get("source_url")
            if not url:
                continue
            text = f"{article.get('title') or ''}\n{article.get('summary') or ''}"
            if link_text(text, search_dict):
                to_link.append(article)
            else:
                gated_out.add(url)
        logger.info(json.dumps({
            "event": "linker_gate_stats",
            "articles": len(articles),
            "forwarded": len(to_link),
            "skipped": len(gated_out),
        }))

    # Primary: LLM linker. Falls back to regex per-article on any error.
    out: dict[str, list[EntityLink]] = {}
    if to_link:
        try:
            from services.entity_linker_llm import link_articles_llm
            # omit_failures=True: a per-article failure (timeout, API error,
            # unparseable response) leaves that url OUT of the dict so the
            # regex fallback below can see it. #136 made failure
            # representable and used it for the backfill write path; without
            # it here, a failure still arrived as [] — indistinguishable from
            # the LLM's real "no entities" verdict — and the fallback never
            # fired for it.
            out = await link_articles_llm(to_link, catalog, omit_failures=True)  # type: ignore[arg-type]
            # Denominator is *unique urls*, not len(to_link). link_articles_llm
            # keys by source_url, so two articles sharing one collapse to a
            # single entry — and the deduplicator only dedupes intra-batch by
            # content_hash, so a batch really can carry the same url twice.
            # Against len(to_link) that read as "7/8 resolved", i.e. a
            # per-article failure that never happened. Both sides count urls.
            # The difference is the per-article failure count.
            forwarded_urls = {a.get("source_url") for a in to_link if a.get("source_url")}
            logger.info(
                "entity_linker: LLM path resolved %d/%d forwarded articles",
                len(out), len(forwarded_urls),
            )
        except Exception as e:  # noqa: BLE001 — degrade rather than block the pipeline
            logger.warning(
                "entity_linker: LLM path failed (%s) — falling back to regex for all %d",
                e, len(to_link),
            )

    # Gated-out articles are answered directly. They are NOT sent to the regex
    # fallback below: the gate's whole premise is that link_text already ran on
    # them and found nothing, so re-running it would just recompute [].
    for url in gated_out:
        out.setdefault(url, [])

    # For any article the LLM path didn't resolve — no url in its dict because
    # the whole batch raised, or because that one article's call failed and
    # link_articles_llm(omit_failures=True) omitted it — fall back to regex.
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
