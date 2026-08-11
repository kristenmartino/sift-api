"""Roster narrowing: what the LLM linker is allowed to see for one article.

`narrow_catalog` sends ~2.3 catalog rows instead of ~856, so the failure modes
are about what gets left OUT. The costly one is silent: a roster missing a row
cannot produce that link, and the article just quietly has fewer chips.

The end-to-end accuracy question is answered by scripts/eval_linker_roster.py
against prod (82.9% overall precision vs the full roster's 80.9%). These are the
structural invariants that hold regardless of any model's behaviour.
"""
from __future__ import annotations

import pytest

from services import entity_linker_llm
from services.entity_linker import narrow_catalog


CATALOG = [
    {"type": "politician", "canonical_id": "S000148",
     "primary_name": "Chuck Schumer", "aliases": []},
    {"type": "politician", "canonical_id": "J000294",
     "primary_name": "Mike Johnson", "aliases": []},
    {"type": "politician", "canonical_id": "J000999",
     "primary_name": "Eddie Johnson", "aliases": []},
    {"type": "org", "canonical_id": "brookings-institution",
     "primary_name": "Brookings Institution", "aliases": []},
    {"type": "outlet", "canonical_id": "reuters",
     "primary_name": "Reuters", "aliases": []},
]


def _link(etype, cid, surface="x"):
    return {"type": etype, "canonical_id": cid, "surface_form": surface}


def _refs(rows):
    return {(r["type"], r["canonical_id"]) for r in rows}


def test_keeps_the_matched_rows():
    out = narrow_catalog(CATALOG, [_link("politician", "S000148")])
    assert ("politician", "S000148") in _refs(out)


def test_drops_everything_unmatched():
    """The whole point. 856 rows down to the handful in play."""
    out = narrow_catalog(CATALOG, [_link("politician", "S000148")])
    assert ("org", "brookings-institution") not in _refs(out)
    assert ("outlet", "reuters") not in _refs(out)


def test_keeps_same_surname_siblings():
    """Two catalog Johnsons: matching one must carry the other, or the model
    cannot tell them apart and has no way to know it is choosing."""
    out = narrow_catalog(CATALOG, [_link("politician", "J000294")])
    assert _refs(out) >= {("politician", "J000294"), ("politician", "J000999")}


def test_single_word_names_do_not_drag_in_the_catalog():
    """Surnames are taken from multi-word names only. A one-word primary_name
    ('Reuters') has no surname, and treating its only token as one would pull
    in every row that happens to end with it."""
    out = narrow_catalog(CATALOG, [_link("outlet", "reuters")])
    assert _refs(out) == {("outlet", "reuters")}


def test_no_matches_yields_no_roster():
    assert narrow_catalog(CATALOG, []) == []


def test_preserves_catalog_order():
    """Roster row order has to be stable across articles that narrow to the
    same set, or every call is a fresh prompt prefix and the ephemeral cache
    never hits."""
    matches = [_link("politician", "J000999"), _link("politician", "S000148")]
    out = narrow_catalog(CATALOG, matches)
    assert [r["canonical_id"] for r in out] == ["S000148", "J000294", "J000999"]


def test_unknown_match_does_not_explode():
    """A canonical_id absent from the catalog (stale search_dict, mid-reseed)
    must be ignored rather than raising — this runs inside the pipeline."""
    out = narrow_catalog(CATALOG, [_link("politician", "NOPE-000")])
    assert _refs(out) == set()


# ── wiring: link_articles_llm's per-article roster ────────────────────


@pytest.mark.asyncio
async def test_per_article_roster_is_used(monkeypatch):
    """Each article must be called with its OWN narrowed roster."""
    monkeypatch.setattr(
        "services.entity_linker_llm.log_usage", lambda *a, **kw: None)
    seen: dict[str, int] = {}

    async def _fake(title, summary, catalog, **kw):
        seen[title] = len(catalog)
        return []

    monkeypatch.setattr(entity_linker_llm, "link_text_llm", _fake)
    monkeypatch.setattr(entity_linker_llm, "_client", lambda: object())

    articles = [
        {"source_url": "u1", "title": "a1", "summary": "s"},
        {"source_url": "u2", "title": "a2", "summary": "s"},
    ]
    await entity_linker_llm.link_articles_llm(
        articles, CATALOG,
        catalogs={"u1": CATALOG[:1], "u2": CATALOG[:3]},
    )
    assert seen == {"a1": 1, "a2": 3}


@pytest.mark.asyncio
async def test_missing_or_empty_roster_falls_back_to_full(monkeypatch):
    """A partial mapping is safe, and an EMPTY roster must not short-circuit
    to [] — that would be a failure wearing a verdict's clothes."""
    monkeypatch.setattr(
        "services.entity_linker_llm.log_usage", lambda *a, **kw: None)
    seen: dict[str, int] = {}

    async def _fake(title, summary, catalog, **kw):
        seen[title] = len(catalog)
        return []

    monkeypatch.setattr(entity_linker_llm, "link_text_llm", _fake)
    monkeypatch.setattr(entity_linker_llm, "_client", lambda: object())

    articles = [
        {"source_url": "u1", "title": "unmapped", "summary": "s"},
        {"source_url": "u2", "title": "empty", "summary": "s"},
    ]
    await entity_linker_llm.link_articles_llm(
        articles, CATALOG, catalogs={"u2": []},
    )
    assert seen == {"unmapped": len(CATALOG), "empty": len(CATALOG)}
