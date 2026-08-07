"""Tests for services/entity_linker.py (Phase 3.G)."""
from __future__ import annotations

import logging
from collections import Counter
from unittest.mock import AsyncMock

import pytest

from services.entity_linker import (
    _REGEX_INELIGIBLE_NAMES,
    build_catalog,
    build_search_dict,
    link_text,
    nickname_variants,
    politician_aliases,
)


# ── politician_aliases ──────────────────────────────────────
#
# Policy (current): no aliases. We deliberately drop last-name-only
# matching because common-noun surnames in the curated roster (Cloud,
# Self, Case, Strong, Banks, Hill, Young, Downing, Green, ...)
# constantly false-match in news copy. See the module docstring for the
# precision-vs-recall trade-off.
#
# Tests assert the no-alias behavior holds across the input shapes the
# function previously branched on, so a future Phase 3.G.2 reverse can't
# silently regress.


def test_politician_aliases_returns_empty_for_unique_lastname():
    """Even unique last names get no alias (was: returned [last])."""
    freq = Counter({"schumer": 1, "warren": 1, "jones": 3})
    assert politician_aliases("Chuck Schumer", freq) == []
    assert politician_aliases("Elizabeth Warren", freq) == []


def test_politician_aliases_returns_empty_for_ambiguous_lastname():
    """Ambiguous last names also get no alias (unchanged behavior)."""
    freq = Counter({"jones": 3, "smith": 2})
    assert politician_aliases("Mary Jones", freq) == []
    assert politician_aliases("Bob Smith", freq) == []


def test_politician_aliases_returns_empty_for_single_token_name():
    """Single-token names get no alias (unchanged behavior)."""
    freq = Counter({"madonna": 1})
    assert politician_aliases("Madonna", freq) == []


def test_politician_aliases_returns_empty_for_short_lastname():
    """Short last names get no alias (unchanged behavior)."""
    freq = Counter({"wu": 1})
    assert politician_aliases("Michelle Wu", freq) == []


def test_politician_aliases_no_false_match_on_common_noun_surnames():
    """Regression: 'Cloud', 'Self', 'Case', 'Strong', 'Banks', 'Green',
    'Young', 'Downing' all live in the real roster. The pre-fix policy
    produced last-name aliases for each, which then false-matched in
    news copy ('cloud computing', 'the case involves', 'Cloud AI',
    'China Asks Banks to Pause', 'green steel', 'young people',
    'self-improving AI', 'downing power lines'). The fix is no aliases."""
    freq = Counter({s: 1 for s in (
        "cloud", "self", "case", "strong", "banks",
        "green", "young", "downing",
    )})
    for full in (
        "Michael Cloud", "Keith Self", "Ed Case", "Dale Strong",
        "Jim Banks", "Al Green", "Todd Young", "Troy Downing",
    ):
        assert politician_aliases(full, freq) == [], full


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


def test_build_catalog_politicians_have_no_aliases():
    """Politician rows carry no *derived surname* aliases — only the full
    canonical name is searchable. See politician_aliases policy note.

    Scope note: names containing a bioguide nickname parenthetical are the one
    exception and are covered separately below; none of these inputs have one.
    """
    catalog = build_catalog(
        outlets=[],
        politicians=[
            {"bioguide_id": "S000148", "name": "Chuck Schumer"},
            {"bioguide_id": "J001", "name": "Mary Jones"},
            {"bioguide_id": "J002", "name": "Bob Jones"},
        ],
        orgs=[],
        bills=[],
    )
    for row in catalog:
        if row["type"] == "politician":
            assert row["aliases"] == [], row["primary_name"]


# ── regex pre-gate on the LLM linker ────────────────────────
#
# link_articles_llm is one realtime call per article and the largest line item
# in the repo. The gate forwards an article only when the free regex matcher
# finds a candidate, on the premise that the LLM disambiguates candidates
# rather than discovering names that never appear. These pin the routing; the
# recall cost of the premise is measured separately by
# scripts/eval_linker_gate.py (98.11% over 12,690 articles).

_GATE_CATALOG_SQL = {
    "outlet_profiles": [{"slug": "cnn", "name": "CNN"}],
    "politician_profiles": [{"bioguide_id": "S000148", "name": "Chuck Schumer"}],
    "org_profiles": [],
    "bill_profiles": [],
    "entity_aliases": [{"alias": "cnn", "entity_type": "outlet", "canonical_id": "cnn"}],
}


def _gate_pool() -> AsyncMock:
    """Pool whose five catalog queries each return the right fixture."""
    async def fetch(sql, *args):
        for table, rows in _GATE_CATALOG_SQL.items():
            if table in sql:
                return rows
        raise AssertionError(f"unexpected query: {sql}")

    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    return pool


async def _run_link_articles(articles, llm, monkeypatch, *, gate=True):
    from app.config import settings
    from services import entity_linker, entity_linker_llm

    monkeypatch.setattr(settings, "entity_linker_regex_gate_enabled", gate)
    monkeypatch.setattr(entity_linker_llm, "link_articles_llm", llm)
    monkeypatch.setattr("app.db.get_pool", AsyncMock(return_value=_gate_pool()))
    return await entity_linker.link_articles(articles)


def _article(url: str, title: str) -> dict:
    return {"source_url": url, "source_name": "Outlet", "title": title, "summary": ""}


@pytest.mark.asyncio
async def test_gate_forwards_only_articles_with_a_regex_candidate(monkeypatch):
    hit = _article("https://e.com/1", "She told CNN the vote was close")
    miss = _article("https://e.com/2", "Local bakery wins a prize")
    llm = AsyncMock(return_value={hit["source_url"]: [
        {"type": "outlet", "canonical_id": "cnn", "surface_form": "CNN"},
    ]})

    out = await _run_link_articles([hit, miss], llm, monkeypatch)

    forwarded = [a["source_url"] for a in llm.await_args.args[0]]
    assert forwarded == [hit["source_url"]]
    assert out[hit["source_url"]][0]["canonical_id"] == "cnn"
    # The skipped article still gets an answer — absent would break store_node.
    assert out[miss["source_url"]] == []


@pytest.mark.asyncio
async def test_gate_disabled_forwards_everything(monkeypatch):
    hit = _article("https://e.com/1", "She told CNN the vote was close")
    miss = _article("https://e.com/2", "Local bakery wins a prize")
    llm = AsyncMock(return_value={})

    await _run_link_articles([hit, miss], llm, monkeypatch, gate=False)

    forwarded = [a["source_url"] for a in llm.await_args.args[0]]
    assert forwarded == [hit["source_url"], miss["source_url"]]


@pytest.mark.asyncio
async def test_gate_skips_the_llm_entirely_when_nothing_qualifies(monkeypatch):
    """An all-miss batch must not pay for an empty request."""
    misses = [_article(f"https://e.com/{i}", "Local bakery wins a prize") for i in (1, 2)]
    llm = AsyncMock(return_value={})

    out = await _run_link_articles(misses, llm, monkeypatch)

    llm.assert_not_called()
    assert all(out[a["source_url"]] == [] for a in misses)


@pytest.mark.asyncio
async def test_llm_verdict_overrides_the_regex_candidate(monkeypatch):
    """The regex only nominates. Disambiguation is why the LLM is still called,
    so a forwarded article must keep the LLM's answer — including [] when the
    LLM decides the candidate was a false positive."""
    article = _article("https://e.com/1", "The CNN Tower dominates the skyline")
    llm = AsyncMock(return_value={article["source_url"]: []})

    out = await _run_link_articles([article], llm, monkeypatch)

    llm.assert_awaited_once()
    assert out[article["source_url"]] == []


@pytest.mark.asyncio
async def test_llm_failure_still_degrades_to_regex_for_forwarded_articles(monkeypatch):
    """The pre-existing fallback must survive the gate: a forwarded article
    whose LLM call blew up falls back to its regex links, not to []."""
    article = _article("https://e.com/1", "She told CNN the vote was close")
    llm = AsyncMock(side_effect=RuntimeError("API down"))

    out = await _run_link_articles([article], llm, monkeypatch)

    assert [link["canonical_id"] for link in out[article["source_url"]]] == ["cnn"]


@pytest.mark.asyncio
async def test_per_article_llm_failure_falls_back_to_regex(monkeypatch):
    """The bug this pins: link_articles_llm stored [] for an article whose own
    call timed out, which is indistinguishable from the LLM answering "no
    entities" — so `if url not in out` never fired and the article shipped
    with no chips at all. #136 made failure representable; passing
    omit_failures=True here is what leaves the failed url absent.

    Both halves ride in one batch because the distinction *is* the fix:
    absent -> regex links; present-and-[] -> the LLM's verdict stands (see
    test_llm_verdict_overrides_the_regex_candidate)."""
    answered = _article("https://e.com/1", "The CNN Tower dominates the skyline")
    failed = _article("https://e.com/2", "Chuck Schumer told CNN the vote was close")
    llm = AsyncMock(return_value={answered["source_url"]: []})  # `failed` omitted

    out = await _run_link_articles([answered, failed], llm, monkeypatch)

    # link_articles must actually *ask* for the omitting behavior — an
    # AsyncMock drops a missing url either way, so without this assertion the
    # test would pass against a link_articles that never opted in.
    assert llm.await_args.kwargs.get("omit_failures") is True
    assert [x["canonical_id"] for x in out[failed["source_url"]]] == ["cnn", "S000148"]
    assert out[answered["source_url"]] == []


@pytest.mark.asyncio
async def test_every_url_bearing_article_gets_a_key(monkeypatch):
    """store_node does entity_links[source_url], so omit_failures must not
    leak past link_articles. Gated-out, LLM-answered and LLM-failed articles
    all come back keyed."""
    gated = _article("https://e.com/1", "Local bakery wins a prize")
    answered = _article("https://e.com/2", "The CNN Tower dominates the skyline")
    failed = _article("https://e.com/3", "Chuck Schumer told CNN the vote was close")
    llm = AsyncMock(return_value={answered["source_url"]: []})

    out = await _run_link_articles([gated, answered, failed], llm, monkeypatch)

    assert set(out) == {a["source_url"] for a in (gated, answered, failed)}


@pytest.mark.asyncio
async def test_duplicate_source_url_is_not_counted_as_a_failure(monkeypatch, caplog):
    """Two articles can share a source_url — the deduplicator only dedupes
    intra-batch by content_hash — and link_articles_llm keys by url, so they
    collapse to one entry. Counting that against len(to_link) reported a
    per-article failure that never happened: the first prod run after #139
    logged "resolved 7/8 forwarded articles" with no failure behind it.

    Both sides of the ratio must count urls."""
    a1 = _article("https://e.com/dup", "The CNN Tower dominates the skyline")
    a2 = _article("https://e.com/dup", "Chuck Schumer spoke to CNN today")
    llm = AsyncMock(return_value={"https://e.com/dup": []})

    with caplog.at_level(logging.INFO, logger="sift-api.entity_linker"):
        await _run_link_articles([a1, a2], llm, monkeypatch)

    resolved = [r.getMessage() for r in caplog.records if "LLM path resolved" in r.getMessage()]
    assert resolved == ["entity_linker: LLM path resolved 1/1 forwarded articles"]


# ── curated short-key exemption ─────────────────────────────
#
# _MIN_KEY_LENGTH = 4 is a proxy for "this key was derived, so nobody vouched
# for it". Applied to curated rows it was suppressing the most-mentioned
# outlets in the corpus: BBC (1,068 articles), CNN (397), NPR (241). Curated
# rows get _MIN_CURATED_KEY_LENGTH instead. These pin both halves — that the
# exemption works, and that it does NOT leak to derived keys.


def _catalog_with_curated_alias(alias: str):
    return build_catalog(
        outlets=[{"slug": "cnn", "name": "CNN"}],
        politicians=[],
        orgs=[],
        bills=[],
        aliases=[{"alias": alias, "entity_type": "outlet", "canonical_id": "cnn"}],
    )


def test_curated_short_alias_survives_the_length_floor():
    catalog = _catalog_with_curated_alias("cnn")
    row = next(r for r in catalog if r["type"] == "outlet")
    assert row["curated"] == ["cnn"]
    # Still present in `aliases` too — _format_catalog_block reads that, so
    # splitting them out would silently starve the LLM path.
    assert "cnn" in row["aliases"]
    assert build_search_dict(catalog)["cnn"] == ("outlet", "cnn")


def test_derived_short_key_is_still_dropped():
    """The #40 guard. Only curated rows earn the lower floor."""
    catalog = build_catalog(
        outlets=[],
        politicians=[],
        orgs=[{"slug": "abc-org", "name": "ABC"}],  # 3 chars, nobody vouched
        bills=[],
    )
    assert "abc" not in build_search_dict(catalog)


def test_curated_alias_below_the_curated_floor_is_dropped():
    """One character cannot be an unambiguous entity reference, curated or not."""
    assert "x" not in build_search_dict(_catalog_with_curated_alias("x"))


def test_curated_alias_that_is_a_stopword_is_still_dropped():
    """The floor is relaxed; the stopword list is not."""
    assert "the" not in build_search_dict(_catalog_with_curated_alias("the"))


def test_short_outlet_links_end_to_end_via_curated_alias():
    """CNN's canonical name is itself 3 chars, so primary_name alone can never
    match — only the self-referential curated row makes it searchable."""
    catalog = _catalog_with_curated_alias("cnn")
    links = link_text("She told CNN the vote was close.", build_search_dict(catalog))
    assert [link["canonical_id"] for link in links] == ["cnn"]


# ── regex-ineligible catalog names ──────────────────────────
#
# Catalog names that are also ordinary English. Neither _STOPWORDS (single
# words only) nor _MIN_KEY_LENGTH (says nothing about "foreign policy") caught
# these, and a regex-mode backfill put 5,838 bad chips on 3,872 prod articles
# from five of them. The contract has two halves: they never reach the regex
# dictionary, and they DO stay in the catalog the LLM linker reads.


def _outlet_catalog(slug: str, name: str, aliases: list[dict] | None = None):
    return build_catalog(
        outlets=[{"slug": slug, "name": name}],
        politicians=[], orgs=[], bills=[], aliases=aliases,
    )


@pytest.mark.parametrize(("slug", "name", "prose"), [
    ("the-nation", "The Nation", "Sales of the nation's top prospects rose."),
    ("nature", "Nature", "The report described the nature of the meeting."),
    ("foreign-policy", "Foreign Policy", "It reflects U.S. foreign policy priorities."),
    ("foreign-affairs", "Foreign Affairs", "He testified to the House Foreign Affairs Committee."),
    ("reason", "Reason", "Layoffs were the top cited reason."),
    ("slate", "Slate", "Telemundo announced its unscripted slate."),
    ("the-verge", "The Verge", "Coventry City is on the verge of promotion."),
    ("the-atlantic", "The Atlantic", "The storm crossed the Atlantic Ocean."),
    ("the-times", "The Times", "Here are all the times Congress recessed."),
    ("the-free-press", "The Free Press", "An oligarchic takeover of the free press."),
    ("stat-news", "STAT", "Red Sox's Most Absurd Stat Behind Pre-All-Star Surge"),
    ("stat-news", "STAT", "Lakers fans will love this Walker Kessler stat"),
])
def test_regex_ineligible_name_produces_no_chip(slug, name, prose):
    """The five measured offenders plus the five the audit added, plus `stat`.

    `stat` is the one that a 4-char floor lets through and that a pooled
    false-positive rate hides: 762 of its 873 stored chips were STAT News
    naming itself, which dilutes 111-of-111 wrong on everyone else's copy
    down to a respectable-looking 12.7%.
    """
    search_dict = build_search_dict(_outlet_catalog(slug, name))
    assert link_text(prose, search_dict) == []


def test_regex_ineligible_names_are_absent_from_the_search_dict():
    catalog = _outlet_catalog("the-nation", "The Nation")
    assert build_search_dict(catalog) == {}


def test_regex_ineligible_row_stays_in_the_llm_catalog():
    """The point of the blocklist: withheld from regex, NOT from the catalog.
    entity_linker_llm reads these rows, so context can still resolve them."""
    catalog = _outlet_catalog("nature", "Nature")
    row = next(r for r in catalog if r["type"] == "outlet")
    assert row["primary_name"] == "Nature"
    assert row["canonical_id"] == "nature"


def test_curated_alias_cannot_reintroduce_an_ineligible_name():
    """Same posture as _STOPWORDS: the alias table is not an override."""
    catalog = _outlet_catalog(
        "nature", "Nature",
        aliases=[{"alias": "Nature", "entity_type": "outlet", "canonical_id": "nature"}],
    )
    assert "nature" not in build_search_dict(catalog)


def test_a_narrower_curated_alias_is_the_escape_hatch():
    """Blocking the bare name does not block a precise one — this is how a
    curator restores recall without reopening the prose collision.

    Uses the forms actually curated in data/entity_aliases.csv. This asserted
    on "the journal Nature" until 2026-08-06, a phrase that occurs zero times
    in the corpus — a green test for an alias nobody could ever match.
    """
    catalog = _outlet_catalog(
        "nature", "Nature",
        aliases=[{"alias": a, "entity_type": "outlet", "canonical_id": "nature"}
                 for a in ("nature study", "study in Nature")],
    )
    search_dict = build_search_dict(catalog)
    assert link_text("A Nature study found the algorithm skewed.",
                     search_dict)[0]["canonical_id"] == "nature"
    assert link_text("Researchers published a study in Nature.",
                     search_dict)[0]["canonical_id"] == "nature"
    assert link_text("The nature of the meeting was unclear.", search_dict) == []


def test_a_narrower_alias_still_does_not_reach_a_sub_brand():
    """"published in Nature" is deliberately NOT curated: 62 of its 88
    third-party corpus matches continue into a different journal, and a
    whole-phrase key matches on the boundary after "Nature". Pins that the
    forms we did curate do not have that failure mode."""
    catalog = _outlet_catalog(
        "nature", "Nature",
        aliases=[{"alias": "nature study", "entity_type": "outlet",
                  "canonical_id": "nature"}],
    )
    search_dict = build_search_dict(catalog)
    assert link_text("The finding, published in Nature Communications, may help.",
                     search_dict) == []


def test_stat_news_stays_linkable_through_its_curated_alias():
    """The condition the `stat` blocklist entry was accepted under.

    Blocking the bare name makes the outlet unmatchable by its own canonical
    name — "STAT reported" no longer chips, and that is the accepted cost
    (the corpus held zero third-party mentions of STAT in any casing when it
    was measured). The curated two-word form is what keeps it linkable at
    all, and it clears _MIN_CURATED_KEY_LENGTH comfortably.
    """
    catalog = _outlet_catalog(
        "stat-news", "STAT",
        aliases=[{"alias": "STAT News", "entity_type": "outlet",
                  "canonical_id": "stat-news"}],
    )
    search_dict = build_search_dict(catalog)
    assert "stat" not in search_dict
    assert "stat news" in search_dict

    linked = link_text("First reported by STAT News on Tuesday.", search_dict)
    assert [link["canonical_id"] for link in linked] == ["stat-news"]
    assert link_text("One Kenley Jansen Stat Proves It", search_dict) == []


def test_unlisted_lookalike_names_are_untouched():
    """Measured and deliberately kept: blocking these would cost far more real
    links than it saves. Guards against over-broad additions to the blocklist."""
    for slug, name, real in [
        ("the-athletic", "The Athletic", "Ranked in the top 20 by The Athletic."),
        ("the-hill", "The Hill", "According to The Hill, the vote slipped."),
        ("the-guardian", "The Guardian", "Records reviewed by The Guardian show delays."),
        ("variety", "Variety", "Variety published its annual New Leaders list."),
    ]:
        search_dict = build_search_dict(_outlet_catalog(slug, name))
        assert [link["canonical_id"] for link in link_text(real, search_dict)] == [slug]


def test_blocklist_entries_are_normalized():
    """A capitalized or padded entry would silently never match, since the
    check runs against the output of _normalize()."""
    for entry in _REGEX_INELIGIBLE_NAMES:
        assert entry == entry.strip().lower()
        assert "  " not in entry


# ── nickname_variants ───────────────────────────────────────
#
# The bioguide roster stores nicknames inline ("Charles (Chuck) Edwards"), a
# string journalism never prints. Measured 2026-08-05 by
# scripts/eval_linker_gate.py, E000246 alone accounted for 11 of ~110 linker
# misses. These are the five real roster shapes as of that date, plus the
# guards that keep this from reopening the #40 surname hazard.


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Charles (Chuck) Edwards", ["Charles Edwards", "Chuck Edwards"]),
        ("Gabriel (Gabe) Vasquez", ["Gabriel Vasquez", "Gabe Vasquez"]),
        ("James (Jim) Moylan", ["James Moylan", "Jim Moylan"]),
        ("Nicole (Nikki) Budzinski", ["Nicole Budzinski", "Nikki Budzinski"]),
        ("Zachary (Zach) Nunn", ["Zachary Nunn", "Zach Nunn"]),
    ],
)
def test_nickname_variants_expands_both_readings(name, expected):
    assert nickname_variants(name) == expected


def test_nickname_variants_noop_without_a_parenthetical():
    assert nickname_variants("Chuck Schumer") == []
    assert nickname_variants("") == []


def test_nickname_variants_never_emits_a_bare_surname():
    """The two-token floor IS the #40 guard. A reading that collapses to one
    token is exactly the common-noun hazard politician_aliases refuses."""
    # "Edwards" alone would be the dropped reading here.
    assert nickname_variants("(Chuck) Edwards") == ["Chuck Edwards"]
    # Nothing survives: both readings are single tokens.
    assert nickname_variants("(Chuck)") == []


def test_build_catalog_attaches_nickname_variants_to_politicians():
    catalog = build_catalog(
        outlets=[],
        politicians=[{"bioguide_id": "E000246", "name": "Charles (Chuck) Edwards"}],
        orgs=[],
        bills=[],
    )
    row = next(r for r in catalog if r["type"] == "politician")
    assert row["primary_name"] == "Charles (Chuck) Edwards"
    assert set(row["aliases"]) == {"Charles Edwards", "Chuck Edwards"}


def test_nickname_expansion_is_politician_only():
    """outlet_profiles carries "Science (AAAS)", where the parenthetical is an
    acronym, not a nickname. Expanding it would put the bare key "Science" in
    the search dict and chip a large fraction of the corpus."""
    catalog = build_catalog(
        outlets=[{"slug": "science", "name": "Science (AAAS)"}],
        politicians=[],
        orgs=[{"slug": "acme", "name": "Acme (Holdings)"}],
        bills=[],
    )
    for row in catalog:
        assert row["aliases"] == [], row["primary_name"]
    assert "science" not in build_search_dict(catalog)


def test_chuck_edwards_links_end_to_end():
    """The miss this whole change exists to fix."""
    catalog = build_catalog(
        outlets=[],
        politicians=[{"bioguide_id": "E000246", "name": "Charles (Chuck) Edwards"}],
        orgs=[],
        bills=[],
    )
    links = link_text(
        "Rep. Chuck Edwards defended the vote on Tuesday.",
        build_search_dict(catalog),
    )
    assert [link["canonical_id"] for link in links] == ["E000246"]
    assert links[0]["surface_form"] == "Chuck Edwards"


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
    """Same entity matched via two keys (e.g., bill name + bill_id) → single
    link. (Politicians no longer get aliases, but bills still do —
    short_title + bill_id, plus year-stripped form.)"""
    d = _dict({
        "inflation reduction act of 2022": ("bill", "hr-5376-117"),
        "inflation reduction act": ("bill", "hr-5376-117"),
        "hr-5376-117": ("bill", "hr-5376-117"),
    })
    out = link_text(
        "The Inflation Reduction Act of 2022, also called the Inflation "
        "Reduction Act, is hr-5376-117.",
        d,
    )
    assert len(out) == 1
    assert out[0]["canonical_id"] == "hr-5376-117"


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


# ── link_text: longest-match-wins ─────────────────────────────
#
# Every key used to be matched independently and every match kept, so a
# short key nested inside a longer name fired on the same span: "The
# Library of Congress opened an exhibit" produced a wrong
# `united-states-congress` chip next to the correct `library-of-congress`
# one. That blocked the two highest-volume curated aliases ("congress",
# 1245 articles; "postal service", which collides with the Postal Service
# Reform Act). Overlapping spans now resolve to the longest match.

_NESTED = _dict({
    "congress": ("org", "united-states-congress"),
    "library of congress": ("org", "library-of-congress"),
    "postal service": ("org", "united-states-postal-service"),
    "postal service reform act": ("bill", "hr-3076-117"),
})


def test_link_text_longer_key_wins_the_span():
    """The motivating case: only the Library, not Congress."""
    out = link_text("The Library of Congress opened an exhibit", _NESTED)
    assert [e["canonical_id"] for e in out] == ["library-of-congress"]


def test_link_text_nested_key_still_links_when_it_stands_alone():
    """The other half: 'congress' must remain useful on its own."""
    out = link_text("Congress passed the bill", _NESTED)
    assert [e["canonical_id"] for e in out] == ["united-states-congress"]


def test_link_text_bill_wins_over_nested_org_name():
    """'Postal Service Reform Act' is the bill, not the Postal Service."""
    out = link_text("The Postal Service Reform Act took effect.", _NESTED)
    assert [e["canonical_id"] for e in out] == ["hr-3076-117"]


def test_link_text_nested_org_links_when_the_bill_is_not_named():
    out = link_text("The Postal Service raised stamp prices.", _NESTED)
    assert [e["canonical_id"] for e in out] == ["united-states-postal-service"]


def test_link_text_resolution_is_per_occurrence_not_per_key():
    """A key that loses one span still owns another. Both entities link."""
    out = link_text(
        "The Library of Congress said Congress had adjourned.", _NESTED,
    )
    assert sorted(e["canonical_id"] for e in out) == [
        "library-of-congress", "united-states-congress",
    ]


def test_link_text_surface_form_is_the_earliest_surviving_match():
    """Display text comes from the winning occurrence, not a suppressed one."""
    out = link_text(
        "Reporting on the Library of Congress, CONGRESS was blamed.", _NESTED,
    )
    forms = {e["canonical_id"]: e["surface_form"] for e in out}
    assert forms["library-of-congress"] == "Library of Congress"
    assert forms["united-states-congress"] == "CONGRESS"


def test_link_text_overlap_resolution_does_not_drop_adjacent_entities():
    """Suppression is span-local: neighbours outside the span are untouched."""
    d = _dict({
        **_NESTED,
        "chuck schumer": ("politician", "S000148"),
    })
    out = link_text(
        "Chuck Schumer toured the Library of Congress.", d,
    )
    assert sorted(e["canonical_id"] for e in out) == [
        "S000148", "library-of-congress",
    ]


def test_link_text_equal_length_overlap_is_deterministic():
    """Same-length overlapping keys can't both win; pick one, stably."""
    d = _dict({
        "acme corp": ("org", "acme-a"),
        "corp acme": ("org", "acme-b"),
    })
    first = link_text("The acme corp acme filing", d)
    for _ in range(5):
        assert link_text("The acme corp acme filing", d) == first
    assert len(first) == 1
