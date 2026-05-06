"""Tests for services/entity_linker.py (Phase 3.G)."""
from __future__ import annotations

from collections import Counter

from services.entity_linker import (
    build_catalog,
    build_search_dict,
    link_text,
    politician_aliases,
)


# ── politician_aliases ──────────────────────────────────────


def test_politician_aliases_unique_lastname():
    """Last names that appear once in the catalog become aliases."""
    freq = Counter({"schumer": 1, "warren": 1, "jones": 3})
    assert politician_aliases("Chuck Schumer", freq) == ["Schumer"]
    assert politician_aliases("Elizabeth Warren", freq) == ["Warren"]


def test_politician_aliases_ambiguous_lastname():
    """Last names that appear multiple times stay out of the alias set."""
    freq = Counter({"jones": 3, "smith": 2})
    assert politician_aliases("Mary Jones", freq) == []
    assert politician_aliases("Bob Smith", freq) == []


def test_politician_aliases_single_token_name():
    """Names without a last-name token return no aliases."""
    freq = Counter({"madonna": 1})
    assert politician_aliases("Madonna", freq) == []


def test_politician_aliases_short_lastname():
    """Last names below the min-key length threshold are dropped."""
    # _MIN_KEY_LENGTH = 4; "Wu" is too short to be a useful alias.
    freq = Counter({"wu": 1})
    assert politician_aliases("Michelle Wu", freq) == []


# ── build_catalog ────────────────────────────────────────────


def test_build_catalog_combines_four_sources():
    catalog = build_catalog(
        outlets=[{"slug": "reuters", "name": "Reuters"}],
        politicians=[
            {"bioguide_id": "S000148", "name": "Chuck Schumer"},
            {"bioguide_id": "C001098", "name": "Ted Cruz"},
        ],
        orgs=[{"slug": "brookings-institution", "name": "Brookings Institution"}],
        bills=[
            {
                "bill_id": "hr-5376-117",
                "title": "An Act to provide for…",
                "short_title": "Inflation Reduction Act",
            },
        ],
    )
    types = sorted(r["type"] for r in catalog)
    assert types == ["bill", "org", "outlet", "politician", "politician"]


def test_build_catalog_skips_rows_missing_required_fields():
    catalog = build_catalog(
        outlets=[
            {"slug": "reuters", "name": "Reuters"},
            {"slug": "", "name": "Empty slug"},  # skipped
            {"slug": "bbc", "name": ""},  # skipped
        ],
        politicians=[
            {"bioguide_id": "S000148", "name": "Chuck Schumer"},
            {"bioguide_id": "", "name": "No bioguide"},  # skipped
        ],
        orgs=[],
        bills=[
            {"bill_id": "hr-1-1", "title": "T", "short_title": ""},  # uses title
            {"bill_id": "", "title": "Has title", "short_title": "Short"},  # skipped
        ],
    )
    assert {r["canonical_id"] for r in catalog} == {
        "reuters", "S000148", "hr-1-1",
    }


def test_build_catalog_politician_aliases_applied():
    catalog = build_catalog(
        outlets=[],
        politicians=[
            {"bioguide_id": "S000148", "name": "Chuck Schumer"},  # unique
            {"bioguide_id": "J001", "name": "Mary Jones"},
            {"bioguide_id": "J002", "name": "Bob Jones"},  # ambiguous
        ],
        orgs=[],
        bills=[],
    )
    schumer = next(r for r in catalog if r["canonical_id"] == "S000148")
    mary = next(r for r in catalog if r["canonical_id"] == "J001")
    bob = next(r for r in catalog if r["canonical_id"] == "J002")
    assert "Schumer" in schumer["aliases"]
    assert mary["aliases"] == []  # Jones is shared with Bob
    assert bob["aliases"] == []


def test_build_catalog_bill_uses_short_title_or_falls_back_to_title():
    catalog = build_catalog(
        outlets=[], politicians=[], orgs=[],
        bills=[
            {"bill_id": "hr-1-1", "short_title": "Short", "title": "Long Title"},
            {"bill_id": "hr-2-1", "short_title": "", "title": "Long Title Two"},
        ],
    )
    a = next(r for r in catalog if r["canonical_id"] == "hr-1-1")
    b = next(r for r in catalog if r["canonical_id"] == "hr-2-1")
    assert a["primary_name"] == "Short"
    assert b["primary_name"] == "Long Title Two"


def test_build_catalog_bill_year_stripped_alias():
    """`Foo Act of 2022` short titles get a year-stripped alias so journalism
    that drops the year (very common) still resolves the bill."""
    catalog = build_catalog(
        outlets=[], politicians=[], orgs=[],
        bills=[
            {
                "bill_id": "hr-5376-117",
                "short_title": "Inflation Reduction Act of 2022",
                "title": "An Act to provide for…",
            },
        ],
    )
    row = catalog[0]
    assert row["primary_name"] == "Inflation Reduction Act of 2022"
    assert "Inflation Reduction Act" in row["aliases"]
    assert "hr-5376-117" in row["aliases"]


def test_build_catalog_bill_no_year_no_alias_added():
    """Short titles without trailing 'of YYYY' don't get a year-stripped alias."""
    catalog = build_catalog(
        outlets=[], politicians=[], orgs=[],
        bills=[
            {"bill_id": "hr-1-1", "short_title": "Affordable Care Act", "title": "T"},
        ],
    )
    row = catalog[0]
    # Aliases is just the canonical bill_id, no stripped form.
    assert row["aliases"] == ["hr-1-1"]


# ── build_search_dict ────────────────────────────────────────


def test_build_search_dict_lowercases_keys():
    catalog = build_catalog(
        outlets=[{"slug": "reuters", "name": "Reuters"}],
        politicians=[], orgs=[], bills=[],
    )
    d = build_search_dict(catalog)
    assert "reuters" in d
    assert d["reuters"] == ("outlet", "reuters")


def test_build_search_dict_drops_stopwords_and_short_keys():
    # Defensive — even if "and" got curated as a primary name, drop it.
    catalog = [
        {"type": "org", "canonical_id": "foo", "primary_name": "And", "aliases": []},
        {"type": "org", "canonical_id": "bar", "primary_name": "Hi", "aliases": []},  # too short
        {"type": "org", "canonical_id": "ok", "primary_name": "Brookings Institution", "aliases": []},
    ]
    d = build_search_dict(catalog)  # type: ignore[arg-type]
    assert "and" not in d
    assert "hi" not in d
    assert "brookings institution" in d


def test_build_search_dict_drops_ambiguous_keys():
    """Two entities mapping to the same surface form means the linker
    can't disambiguate — better to drop and miss than to point wrong."""
    catalog = [
        {"type": "org", "canonical_id": "apple-inc", "primary_name": "Apple", "aliases": []},
        {"type": "org", "canonical_id": "apple-records", "primary_name": "Apple", "aliases": []},
    ]
    d = build_search_dict(catalog)  # type: ignore[arg-type]
    assert "apple" not in d


def test_build_search_dict_keeps_aliases_pointing_at_same_canonical():
    """Same entity contributing multiple keys (primary + alias) is fine."""
    catalog = [
        {
            "type": "politician", "canonical_id": "S000148",
            "primary_name": "Chuck Schumer", "aliases": ["Schumer"],
        },
    ]
    d = build_search_dict(catalog)  # type: ignore[arg-type]
    assert d["chuck schumer"] == ("politician", "S000148")
    assert d["schumer"] == ("politician", "S000148")


# ── link_text ─────────────────────────────────────────────────


def _dict(canonical_pairs: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    return canonical_pairs


def test_link_text_word_boundary_match():
    """Match only on whole-word boundaries, not substrings."""
    d = _dict({"reuters": ("outlet", "reuters")})
    # Substring inside another word should NOT match.
    assert link_text("Some non-reuter source said", d) == []
    # Whole-word match works.
    out = link_text("Reuters reported on the deal.", d)
    assert len(out) == 1
    assert out[0]["canonical_id"] == "reuters"


def test_link_text_case_insensitive():
    d = _dict({"chuck schumer": ("politician", "S000148")})
    out = link_text("CHUCK SCHUMER said today.", d)
    assert len(out) == 1
    assert out[0]["surface_form"] == "CHUCK SCHUMER"


def test_link_text_collapses_duplicate_canonicals():
    """Same entity matched via two keys (name + alias) → single link."""
    d = _dict({
        "chuck schumer": ("politician", "S000148"),
        "schumer": ("politician", "S000148"),
    })
    out = link_text("Chuck Schumer met with Schumer", d)
    assert len(out) == 1
    assert out[0]["canonical_id"] == "S000148"


def test_link_text_preserves_original_casing_in_surface_form():
    d = _dict({"brookings institution": ("org", "brookings-institution")})
    out = link_text("BROOKINGS INSTITUTION released a paper.", d)
    assert out[0]["surface_form"] == "BROOKINGS INSTITUTION"


def test_link_text_multiple_distinct_entities():
    d = _dict({
        "chuck schumer": ("politician", "S000148"),
        "ted cruz": ("politician", "C001098"),
        "brookings institution": ("org", "brookings-institution"),
    })
    text = "Chuck Schumer and Ted Cruz responded to the Brookings Institution report."
    out = link_text(text, d)
    canonicals = {e["canonical_id"] for e in out}
    assert canonicals == {"S000148", "C001098", "brookings-institution"}


def test_link_text_stable_sort():
    """Output is ordered by (type, canonical_id) for diff stability."""
    d = _dict({
        "ted cruz": ("politician", "C001098"),
        "chuck schumer": ("politician", "S000148"),
        "reuters": ("outlet", "reuters"),
        "brookings institution": ("org", "brookings-institution"),
    })
    text = "Chuck Schumer, Ted Cruz, Reuters, Brookings Institution all weighed in."
    out = link_text(text, d)
    types = [e["type"] for e in out]
    # Sort order: bill < org < outlet < politician (alphabetical).
    assert types == ["org", "outlet", "politician", "politician"]
    # Within politician, canonical_id alphabetical → C001098 before S000148.
    politicians = [e["canonical_id"] for e in out if e["type"] == "politician"]
    assert politicians == ["C001098", "S000148"]


def test_link_text_empty_inputs():
    assert link_text("", {"reuters": ("outlet", "reuters")}) == []
    assert link_text("Reuters said", {}) == []


def test_link_text_handles_special_regex_chars_in_keys():
    """Bill IDs contain hyphens. Make sure they're escaped in the regex."""
    d = _dict({"hr-5376-117": ("bill", "hr-5376-117")})
    out = link_text("The bill hr-5376-117 was enacted.", d)
    assert len(out) == 1
    assert out[0]["canonical_id"] == "hr-5376-117"
