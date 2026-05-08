"""Tests for services/entity_linker_llm (Phase 3.G.2).

Mocks the Anthropic client so we don't hit the network. Verifies prompt
construction, response parsing, hallucination rejection, and the
cache_control marker on the system prompt block.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.entity_linker_llm import (
    _build_outlet_name_index,
    _build_system_prompt,
    _build_user_prompt,
    _extract_json_array,
    _format_catalog_block,
    _index_catalog,
    _normalize_outlet_name,
    _parse_response,
    _resolve_source_outlet_slug,
    link_text_llm,
)


SAMPLE_CATALOG = [
    {
        "type": "politician",
        "canonical_id": "S000148",
        "primary_name": "Chuck Schumer",
        "aliases": [],
    },
    {
        "type": "politician",
        "canonical_id": "C001035",
        "primary_name": "Susan Collins",
        "aliases": [],
    },
    {
        "type": "org",
        "canonical_id": "brookings-institution",
        "primary_name": "Brookings Institution",
        "aliases": [],
    },
    {
        "type": "bill",
        "canonical_id": "hr-5376-117",
        "primary_name": "Inflation Reduction Act of 2022",
        "aliases": [],
    },
    {
        "type": "outlet",
        "canonical_id": "reuters",
        "primary_name": "Reuters",
        "aliases": [],
    },
]


# ── Catalog formatting ─────────────────────────────────────────────


def test_format_catalog_block_groups_by_type():
    text = _format_catalog_block(SAMPLE_CATALOG)
    # Each type heading appears exactly once.
    for heading in ("POLITICIANS", "ORGANIZATIONS", "BILLS", "OUTLETS"):
        assert text.count(heading) == 1, heading
    # Canonical_id + primary_name on the entry lines.
    assert "S000148 | Chuck Schumer" in text
    assert "brookings-institution | Brookings Institution" in text


def test_format_catalog_block_skips_missing_types():
    """No outlet rows → no OUTLETS heading."""
    catalog = [
        {"type": "politician", "canonical_id": "X", "primary_name": "X Y", "aliases": []},
    ]
    text = _format_catalog_block(catalog)
    assert "POLITICIANS" in text
    assert "OUTLETS" not in text
    assert "ORGANIZATIONS" not in text


# ── Prompts ─────────────────────────────────────────────────────────


def test_build_system_prompt_embeds_catalog():
    p = _build_system_prompt(SAMPLE_CATALOG)
    # Catalog is in the system prompt verbatim.
    assert "S000148 | Chuck Schumer" in p
    # Disambiguation rule is present (uses Susan Collins as the example).
    assert "Susan Collins" in p


def test_build_system_prompt_includes_indirect_reference_guard():
    """Rule 3 — politician tags require a DIRECT reference, not just a
    state name, party label, or chamber. Caught these in prod after PR
    #45 backfill: 'Colorado' → senator Bennet, 'California' → Pelosi
    on a 'blue states aren't getting fire prevention money' article.
    """
    p = _build_system_prompt(SAMPLE_CATALOG)
    assert "DIRECT reference" in p
    # Specific examples should appear so Claude has anchors.
    assert "blue states" in p.lower()
    assert "lawmakers demanded" in p.lower()
    assert "state name" in p.lower()


def test_build_user_prompt_handles_empty_fields():
    """Falls back to placeholders rather than blank prompt."""
    p = _build_user_prompt("", "")
    assert "(untitled)" in p
    assert "(no summary)" in p


def test_build_user_prompt_with_source_includes_self_skip_rule():
    """When source_name is present, tell the LLM to skip the self-source."""
    p = _build_user_prompt(
        "Some headline", "Some summary", source_name="Financial Times",
    )
    assert "Financial Times" in p
    assert "Do NOT tag the article's own source" in p


def test_build_user_prompt_without_source_omits_self_skip_rule():
    """No source_name → no self-skip clause (keeps the prompt lean)."""
    p = _build_user_prompt("Some headline", "Some summary")
    assert "Do NOT tag the article's own source" not in p


# ── Source-outlet resolution ──────────────────────────────────────


def test_normalize_outlet_name_strips_leading_the():
    assert _normalize_outlet_name("The New York Times") == "new york times"
    assert _normalize_outlet_name("New York Times") == "new york times"


def test_normalize_outlet_name_lowercases():
    assert _normalize_outlet_name("Reuters") == "reuters"
    assert _normalize_outlet_name("REUTERS") == "reuters"


def test_build_outlet_name_index_only_outlets():
    """Politicians/orgs/bills don't pollute the outlet name lookup."""
    idx = _build_outlet_name_index(SAMPLE_CATALOG)
    assert idx == {"reuters": "reuters"}


def test_resolve_source_outlet_slug_match():
    idx = _build_outlet_name_index(SAMPLE_CATALOG)
    assert _resolve_source_outlet_slug("Reuters", idx) == "reuters"
    # 'The Reuters' would be unusual, but if it happened, match still works.
    assert _resolve_source_outlet_slug("The Reuters", idx) == "reuters"


def test_resolve_source_outlet_slug_no_match():
    idx = _build_outlet_name_index(SAMPLE_CATALOG)
    assert _resolve_source_outlet_slug("Unknown Outlet", idx) is None
    assert _resolve_source_outlet_slug(None, idx) is None
    assert _resolve_source_outlet_slug("", idx) is None


# ── Self-source filter in _parse_response ────────────────────────


def test_parse_response_drops_self_source_outlet_chip():
    """LLM tagged the article's own source as an outlet → drop it."""
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"outlet","canonical_id":"reuters","surface_form":"Reuters"},'
        '{"type":"politician","canonical_id":"S000148","surface_form":"Chuck Schumer"}]'
    )
    out = _parse_response(text, valid, source_outlet_slug="reuters")
    canonicals = {(e["type"], e["canonical_id"]) for e in out}
    # Outlet chip dropped; politician chip kept.
    assert canonicals == {("politician", "S000148")}


def test_parse_response_keeps_other_outlet_chip_when_filtering_self():
    """Filtering only drops the self-source, not all outlets."""
    catalog = SAMPLE_CATALOG + [{
        "type": "outlet", "canonical_id": "bbc",
        "primary_name": "BBC", "aliases": [],
    }]
    valid = _index_catalog(catalog)
    text = (
        '[{"type":"outlet","canonical_id":"reuters","surface_form":"Reuters"},'
        '{"type":"outlet","canonical_id":"bbc","surface_form":"BBC"}]'
    )
    out = _parse_response(text, valid, source_outlet_slug="reuters")
    canonicals = {e["canonical_id"] for e in out}
    assert canonicals == {"bbc"}


def test_parse_response_no_filter_when_source_slug_none():
    """No source_slug → all valid outlet chips pass through."""
    valid = _index_catalog(SAMPLE_CATALOG)
    text = '[{"type":"outlet","canonical_id":"reuters","surface_form":"Reuters"}]'
    out = _parse_response(text, valid, source_outlet_slug=None)
    assert len(out) == 1
    assert out[0]["canonical_id"] == "reuters"


# ── JSON extraction ────────────────────────────────────────────────


def test_extract_json_array_strict():
    text = '[{"type":"politician","canonical_id":"S000148","surface_form":"Chuck Schumer"}]'
    parsed = _extract_json_array(text)
    assert parsed is not None
    assert parsed[0]["canonical_id"] == "S000148"


def test_extract_json_array_strips_code_fence():
    text = "```json\n[{\"a\":1}]\n```"
    parsed = _extract_json_array(text)
    assert parsed == [{"a": 1}]


def test_extract_json_array_greedy_fallback():
    """Tolerates leading prose."""
    text = "Here you go: [{\"a\":1}] hope that helps"
    parsed = _extract_json_array(text)
    assert parsed == [{"a": 1}]


def test_extract_json_array_returns_none_on_garbage():
    assert _extract_json_array("not json at all") is None
    # Object instead of array → None (we only accept arrays).
    assert _extract_json_array('{"a": 1}') is None


# ── Response validation / hallucination rejection ──────────────────


def test_parse_response_accepts_valid_entries():
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"politician","canonical_id":"S000148","surface_form":"Chuck Schumer"},'
        '{"type":"org","canonical_id":"brookings-institution","surface_form":"Brookings"}]'
    )
    out = _parse_response(text, valid)
    canonicals = {(e["type"], e["canonical_id"]) for e in out}
    assert canonicals == {
        ("politician", "S000148"),
        ("org", "brookings-institution"),
    }


def test_parse_response_drops_unknown_canonical_ids():
    """LLM hallucinated bioguide_id → drop entry, keep the valid one."""
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"politician","canonical_id":"S000148","surface_form":"Chuck Schumer"},'
        '{"type":"politician","canonical_id":"FAKE9999","surface_form":"Made Up"}]'
    )
    out = _parse_response(text, valid)
    assert len(out) == 1
    assert out[0]["canonical_id"] == "S000148"


def test_parse_response_drops_invalid_types():
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"person","canonical_id":"S000148","surface_form":"Chuck Schumer"}]'
    )
    out = _parse_response(text, valid)
    assert out == []


def test_parse_response_drops_missing_fields():
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"politician","canonical_id":"S000148"},'
        '{"type":"politician","surface_form":"Schumer"}]'
    )
    out = _parse_response(text, valid)
    assert out == []


def test_parse_response_dedupes_same_canonical():
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"politician","canonical_id":"S000148","surface_form":"Chuck Schumer"},'
        '{"type":"politician","canonical_id":"S000148","surface_form":"Schumer"}]'
    )
    out = _parse_response(text, valid)
    assert len(out) == 1


def test_parse_response_returns_empty_array_for_empty_array():
    valid = _index_catalog(SAMPLE_CATALOG)
    assert _parse_response("[]", valid) == []


def test_parse_response_returns_empty_for_garbage():
    valid = _index_catalog(SAMPLE_CATALOG)
    assert _parse_response("???", valid) == []


def test_parse_response_stable_sort():
    valid = _index_catalog(SAMPLE_CATALOG)
    text = (
        '[{"type":"politician","canonical_id":"S000148","surface_form":"Schumer"},'
        '{"type":"org","canonical_id":"brookings-institution","surface_form":"Brookings"}]'
    )
    out = _parse_response(text, valid)
    types = [e["type"] for e in out]
    assert types == ["org", "politician"]


# ── End-to-end with mocked client ──────────────────────────────────


def _mock_response(text: str):
    """Build a minimal stand-in for Anthropic's response object."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=80,
    )
    return SimpleNamespace(content=[block], usage=usage)


@pytest.mark.asyncio
async def test_link_text_llm_happy_path(monkeypatch):
    """Mocked Claude returns valid JSON → caller gets a typed link list."""
    # Patch usage_tracker.log_usage to a no-op (it expects DB pool).
    monkeypatch.setattr(
        "services.entity_linker_llm.log_usage", lambda *a, **kw: None,
    )

    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_mock_response(
                '[{"type":"politician","canonical_id":"S000148","surface_form":"Chuck Schumer"}]'
            ))
        )
    )

    out = await link_text_llm(
        title="Schumer urges action",
        summary="The Senate Majority Leader spoke today.",
        catalog=SAMPLE_CATALOG,
        client=fake_client,  # type: ignore[arg-type]
    )
    assert len(out) == 1
    assert out[0]["canonical_id"] == "S000148"

    # Verify the call shape: model + cache_control on system prompt.
    args, kwargs = fake_client.messages.create.call_args
    system = kwargs["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "ROSTER" in system[0]["text"]


@pytest.mark.asyncio
async def test_link_text_llm_returns_empty_on_api_error(monkeypatch):
    """API error → [] (caller can fall back to regex)."""
    monkeypatch.setattr(
        "services.entity_linker_llm.log_usage", lambda *a, **kw: None,
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("API down"))
        )
    )
    out = await link_text_llm(
        title="x", summary="y",
        catalog=SAMPLE_CATALOG, client=fake_client,  # type: ignore[arg-type]
    )
    assert out == []


@pytest.mark.asyncio
async def test_link_text_llm_short_circuits_on_empty_input():
    """No LLM call when both title and summary are blank."""
    out = await link_text_llm(
        title="", summary="",
        catalog=SAMPLE_CATALOG,
    )
    assert out == []


@pytest.mark.asyncio
async def test_link_text_llm_short_circuits_on_empty_catalog():
    """No LLM call when catalog is empty."""
    out = await link_text_llm(
        title="Schumer", summary="said today",
        catalog=[],
    )
    assert out == []


@pytest.mark.asyncio
async def test_link_text_llm_filters_self_source_outlet_end_to_end(monkeypatch):
    """Even if the LLM tags the article's own source, the post-filter
    drops it. Belt-and-suspenders: prompt asks the LLM to skip,
    validator drops if the LLM ignored the rule."""
    monkeypatch.setattr(
        "services.entity_linker_llm.log_usage", lambda *a, **kw: None,
    )

    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_mock_response(
                # LLM "ignored" the self-skip rule and tagged Reuters.
                '[{"type":"outlet","canonical_id":"reuters","surface_form":"Reuters"}]'
            ))
        )
    )

    out = await link_text_llm(
        title="Some headline",
        summary="Some summary text.",
        catalog=SAMPLE_CATALOG,
        source_name="Reuters",
        client=fake_client,  # type: ignore[arg-type]
    )
    # Self-source chip dropped by the suspenders filter.
    assert out == []

    # And the prompt told the LLM about it (the belt).
    args, kwargs = fake_client.messages.create.call_args
    user_msg = kwargs["messages"][0]["content"]
    assert "Reuters" in user_msg
    assert "Do NOT tag the article's own source" in user_msg
