"""OpenSecrets API client — civic-literacy MVP Phase 3.F.2.

Wraps the public OpenSecrets API (`opensecrets.org/api/`) with one method
the politician dossier cares about: top donor industries for the current
cycle. Returns the result mapped into the `IndustryDonation`-shaped JSON
that lands in `politician_profiles.top_industries_current_cycle`.

Activation
----------
Set `OPENSECRETS_API_KEY` in the environment. Without it, every call
returns `None` and the orchestrator script no-ops gracefully — the
politician dossier continues showing the "Not yet enriched" caption.

Free tier: 200 calls/day. The orchestrator
(`scripts/refresh_politician_donors.py`) handles batching and the
cycle-rotation needed to stay under quota across 535 members.

Reference
---------
- API docs:    https://www.opensecrets.org/open-data/api-documentation
- Endpoint:    https://www.opensecrets.org/api/?method=candIndustry
- Response:    XML by default; we request `output=json`. JSON is a flat
               XML-to-JSON conversion so the shape has `@attributes`
               wrappers under `response.industries.industry[*]`.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("sift-api.opensecrets")

OPENSECRETS_API_BASE = "https://www.opensecrets.org/api/"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_TOP_N = 5  # how many industries to keep per politician

# CRP IDs are 9 chars: a letter + 8 digits, e.g. "N00001093" for Schumer.
# Embedded in OpenSecrets URLs as `?cid=N00001093`.
CID_PATTERN = re.compile(r"[?&]cid=([A-Z][0-9]{8})")


def _api_key() -> str | None:
    """Return the configured OpenSecrets API key, or None if unset.

    The orchestrator checks this once at startup and short-circuits the
    whole refresh when the key is absent — no per-politician network
    calls in that case.
    """
    key = os.environ.get("OPENSECRETS_API_KEY", "").strip()
    return key or None


def extract_cid_from_url(url: str | None) -> str | None:
    """Pull the CRP candidate ID out of an OpenSecrets URL.

    The seed CSV stores the ID inside `external_links.opensecrets` as a
    URL like `https://www.opensecrets.org/members-of-congress/summary?cid=N00001093`.
    Returns None for URLs that don't carry a `cid=` param matching the
    9-character CRP shape (letter + 8 digits).
    """
    if not url:
        return None
    match = CID_PATTERN.search(url)
    return match.group(1) if match else None


def _parse_industries(payload: dict[str, Any], top_n: int) -> list[dict[str, Any]]:
    """Map OpenSecrets' `candIndustry` response to our `IndustryDonation[]`
    shape, sorted by total cycle-to-date amount descending, capped at
    `top_n`. Tolerates malformed shapes (returns []).

    Schema per `opensecrets.org/open-data/api-documentation/candIndustry`:

        {
          "response": {
            "industries": {
              "@attributes": { "cand_name": "...", "cid": "...", "cycle": "..." },
              "industry": [
                {
                  "@attributes": {
                    "industry_code": "F10",
                    "industry_name": "Securities & Investment",
                    "indivs":  "1234567",
                    "pacs":    "234567",
                    "total":   "1469134",
                    "rank":    "1"
                  }
                },
                ...
              ]
            }
          }
        }

    `total` is a string of dollars (no commas). We coerce to int; on parse
    failure for any individual entry the entry is dropped.
    """
    if not isinstance(payload, dict):
        return []
    response = payload.get("response")
    if not isinstance(response, dict):
        return []
    industries_block = response.get("industries")
    if not isinstance(industries_block, dict):
        return []
    raw = industries_block.get("industry")
    if raw is None:
        return []
    # The JSON-from-XML translation collapses single-element lists to a
    # bare object. Normalize to a list either way.
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("@attributes")
        if not isinstance(attrs, dict):
            # Some response variants flatten — fall through to the raw entry.
            attrs = entry
        industry_name = attrs.get("industry_name")
        total_str = attrs.get("total")
        if not isinstance(industry_name, str) or not industry_name.strip():
            continue
        try:
            total = int(str(total_str).strip()) if total_str is not None else None
        except (ValueError, TypeError):
            total = None
        if total is None:
            continue
        out.append({
            "industry": industry_name.strip(),
            "amount_usd": total,
        })

    out.sort(key=lambda e: e["amount_usd"], reverse=True)
    return out[:top_n]


async def fetch_top_industries(
    cid: str,
    cycle: str | int = "2024",
    *,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]] | None:
    """Fetch the top donor industries for a candidate at OpenSecrets.

    Returns the list of `{industry, amount_usd}` dicts sorted desc by
    amount, capped at `top_n` (default 5). Returns:

        - None  → API key not configured, or network/HTTP failure
        - []    → API responded but reported no industry data
        - [...] → success

    Caller is responsible for sleeping between calls (free tier: 200/day).

    Pass an `httpx.AsyncClient` to share connection pooling across many
    calls; otherwise a one-shot client is created per call.
    """
    key = api_key or _api_key()
    if not key:
        logger.debug("opensecrets: OPENSECRETS_API_KEY unset; skipping fetch")
        return None

    cid = cid.strip().upper()
    if not cid:
        return None

    params = {
        "method": "candIndustry",
        "cid": cid,
        "cycle": str(cycle),
        "apikey": key,
        "output": "json",
    }

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        try:
            response = await http.get(OPENSECRETS_API_BASE, params=params)
        except httpx.HTTPError as e:
            logger.warning("opensecrets: HTTP error for cid=%s: %s", cid, e)
            return None
        if response.status_code != 200:
            logger.warning(
                "opensecrets: non-200 status %d for cid=%s",
                response.status_code, cid,
            )
            return None
        try:
            payload = response.json()
        except ValueError as e:
            logger.warning("opensecrets: invalid JSON for cid=%s: %s", cid, e)
            return None
    finally:
        if own_client:
            await http.aclose()

    return _parse_industries(payload, top_n)
