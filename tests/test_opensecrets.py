"""Tests for services/opensecrets.py (Phase 3.F.2).

Network-free: uses httpx.MockTransport to inject canned responses.
"""
from __future__ import annotations

import httpx
import pytest

from services.opensecrets import (
    _parse_industries,
    extract_cid_from_url,
    fetch_top_industries,
    OPENSECRETS_API_BASE,
)


# ─── extract_cid_from_url ─────────────────────────────────


def test_extract_cid_from_url_summary_page():
    url = "https://www.opensecrets.org/members-of-congress/summary?cid=N00001093"
    assert extract_cid_from_url(url) == "N00001093"


def test_extract_cid_from_url_with_extra_query_params():
    url = (
        "https://www.opensecrets.org/members-of-congress/contributors"
        "?cid=N00026050&cycle=2024"
    )
    assert extract_cid_from_url(url) == "N00026050"


def test_extract_cid_from_url_no_cid_param():
    url = "https://www.opensecrets.org/members-of-congress"
    assert extract_cid_from_url(url) is None


def test_extract_cid_from_url_malformed_cid():
    """Only the canonical 9-char (letter + 8 digits) form matches."""
    assert extract_cid_from_url("https://example.com?cid=N1234") is None
    assert extract_cid_from_url("https://example.com?cid=12345678N") is None
    assert extract_cid_from_url("https://example.com?cid=") is None


def test_extract_cid_from_url_none_or_empty():
    assert extract_cid_from_url(None) is None
    assert extract_cid_from_url("") is None


# ─── _parse_industries ────────────────────────────────────


def _industry_payload(industries: list[dict]) -> dict:
    """Build a candIndustry-shaped payload for testing."""
    return {
        "response": {
            "industries": {
                "@attributes": {
                    "cand_name": "Test, Test (X)",
                    "cid": "N00000000",
                    "cycle": "2026",
                },
                "industry": industries,
            },
        },
    }


def test_parse_industries_happy_path():
    payload = _industry_payload([
        {
            "@attributes": {
                "industry_name": "Securities & Investment",
                "industry_code": "F10",
                "total": "1469134",
                "rank": "1",
            },
        },
        {
            "@attributes": {
                "industry_name": "Real Estate",
                "industry_code": "F09",
                "total": "620000",
                "rank": "2",
            },
        },
        {
            "@attributes": {
                "industry_name": "Insurance",
                "industry_code": "F09",
                "total": "350000",
                "rank": "3",
            },
        },
    ])
    out = _parse_industries(payload, top_n=5)
    assert out == [
        {"industry": "Securities & Investment", "amount_usd": 1_469_134},
        {"industry": "Real Estate", "amount_usd": 620_000},
        {"industry": "Insurance", "amount_usd": 350_000},
    ]


def test_parse_industries_caps_at_top_n():
    payload = _industry_payload([
        {"@attributes": {"industry_name": f"Industry {i}", "total": str((10 - i) * 1000)}}
        for i in range(10)
    ])
    out = _parse_industries(payload, top_n=3)
    assert len(out) == 3
    # Sorted by total desc — Industry 0 has the highest total (10000).
    assert out[0]["industry"] == "Industry 0"
    assert out[2]["industry"] == "Industry 2"


def test_parse_industries_sorts_descending_when_input_unordered():
    payload = _industry_payload([
        {"@attributes": {"industry_name": "Small", "total": "1000"}},
        {"@attributes": {"industry_name": "Big", "total": "9000"}},
        {"@attributes": {"industry_name": "Medium", "total": "5000"}},
    ])
    out = _parse_industries(payload, top_n=5)
    assert [e["industry"] for e in out] == ["Big", "Medium", "Small"]


def test_parse_industries_handles_single_object_not_array():
    """OpenSecrets' XML-to-JSON converter sometimes flattens single-element
    lists to a bare object — the parser normalizes either shape."""
    payload = _industry_payload([])
    payload["response"]["industries"]["industry"] = {
        "@attributes": {"industry_name": "Solo", "total": "42"},
    }
    out = _parse_industries(payload, top_n=5)
    assert out == [{"industry": "Solo", "amount_usd": 42}]


def test_parse_industries_drops_entries_missing_industry_name():
    payload = _industry_payload([
        {"@attributes": {"industry_name": "", "total": "1000"}},
        {"@attributes": {"industry_name": "  ", "total": "2000"}},
        {"@attributes": {"total": "3000"}},  # no name field
        {"@attributes": {"industry_name": "Good", "total": "4000"}},
    ])
    out = _parse_industries(payload, top_n=5)
    assert out == [{"industry": "Good", "amount_usd": 4000}]


def test_parse_industries_drops_entries_with_unparseable_total():
    payload = _industry_payload([
        {"@attributes": {"industry_name": "Lots", "total": "lots"}},  # not numeric
        {"@attributes": {"industry_name": "Empty", "total": ""}},
        {"@attributes": {"industry_name": "None", "total": None}},
        {"@attributes": {"industry_name": "Real", "total": "1000"}},
    ])
    out = _parse_industries(payload, top_n=5)
    assert out == [{"industry": "Real", "amount_usd": 1000}]


def test_parse_industries_handles_empty_industry_list():
    payload = _industry_payload([])
    out = _parse_industries(payload, top_n=5)
    assert out == []


def test_parse_industries_handles_missing_industry_field():
    payload = {"response": {"industries": {"@attributes": {}}}}  # no `industry` key
    out = _parse_industries(payload, top_n=5)
    assert out == []


@pytest.mark.parametrize("payload", [
    None,
    {},
    "not a dict",
    {"response": "not a dict"},
    {"response": {"industries": "not a dict"}},
    {"response": {"industries": {"industry": "not a list"}}},
])
def test_parse_industries_returns_empty_for_malformed_payloads(payload):
    out = _parse_industries(payload, top_n=5)
    assert out == []


def test_parse_industries_tolerates_flattened_no_attributes():
    """Some response variants flatten the @attributes wrapper. The parser
    falls back to reading from the entry directly in that case."""
    payload = _industry_payload([])
    payload["response"]["industries"]["industry"] = [
        {"industry_name": "Direct", "total": "5000"},  # no @attributes
    ]
    out = _parse_industries(payload, top_n=5)
    assert out == [{"industry": "Direct", "amount_usd": 5000}]


# ─── fetch_top_industries (mocked HTTP) ────────────────────


@pytest.fixture
def mock_client_factory():
    """Build an httpx.AsyncClient bound to a MockTransport that returns
    the given payload for any candIndustry request."""

    def _factory(payload: dict, status: int = 200) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport, timeout=5)

    return _factory


@pytest.mark.asyncio
async def test_fetch_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENSECRETS_API_KEY", raising=False)
    out = await fetch_top_industries("N00001093")
    assert out is None


@pytest.mark.asyncio
async def test_fetch_happy_path_with_mock_transport(mock_client_factory, monkeypatch):
    monkeypatch.setenv("OPENSECRETS_API_KEY", "test-key-not-real")
    payload = _industry_payload([
        {"@attributes": {"industry_name": "Tech", "total": "500000"}},
        {"@attributes": {"industry_name": "Finance", "total": "1000000"}},
    ])
    async with mock_client_factory(payload) as client:
        out = await fetch_top_industries("N00001093", client=client)
    # Sorted by total desc → Finance first.
    assert out == [
        {"industry": "Finance", "amount_usd": 1_000_000},
        {"industry": "Tech", "amount_usd": 500_000},
    ]


@pytest.mark.asyncio
async def test_fetch_returns_none_on_non_200(mock_client_factory, monkeypatch):
    monkeypatch.setenv("OPENSECRETS_API_KEY", "test-key-not-real")
    async with mock_client_factory({}, status=403) as client:
        out = await fetch_top_industries("N00001093", client=client)
    assert out is None


@pytest.mark.asyncio
async def test_fetch_returns_none_on_invalid_json(monkeypatch):
    """Non-JSON response body → parser bails to None, no exception."""
    monkeypatch.setenv("OPENSECRETS_API_KEY", "test-key-not-real")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await fetch_top_industries("N00001093", client=client)
    assert out is None


@pytest.mark.asyncio
async def test_fetch_returns_none_for_empty_cid(monkeypatch):
    monkeypatch.setenv("OPENSECRETS_API_KEY", "test-key-not-real")
    out = await fetch_top_industries("   ")
    assert out is None


@pytest.mark.asyncio
async def test_fetch_passes_correct_query_params(monkeypatch):
    """Verify the request is shaped the way OpenSecrets' API documents."""
    monkeypatch.setenv("OPENSECRETS_API_KEY", "test-key-not-real")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=_industry_payload([]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await fetch_top_industries("n00001093", cycle=2024, client=client, api_key="explicit-key")

    assert captured["url"].startswith(OPENSECRETS_API_BASE)
    assert captured["params"]["method"] == "candIndustry"
    assert captured["params"]["cid"] == "N00001093"  # uppercased
    assert captured["params"]["cycle"] == "2024"
    assert captured["params"]["apikey"] == "explicit-key"
    assert captured["params"]["output"] == "json"
