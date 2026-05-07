"""Scrape GovTrack for all current Congress members → politician_profiles.csv.

One-shot tooling for civic-literacy Phase 3.B. Fetches every Senator and
Representative currently serving, normalizes the data into our schema,
and writes a fresh `data/politician_profiles.csv`.

Run from sift-api root:
    ./.venv/bin/python3 scripts/scrape_govtrack.py
    ./.venv/bin/python3 scripts/scrape_govtrack.py --output data/politician_profiles.csv

GovTrack's API is free, no key required. The whole scrape is three
paginated requests (538 total records / 200 per page). We do NOT fetch
committee assignments — that's a separate per-member endpoint and would
mean hundreds of extra requests. Phase 3.E (manual or automated) is
where committees + donor industries + interest-group ratings get
populated.

Idempotent: re-running overwrites the CSV. **Hand-curated `committees`,
`notes`, and any non-GovTrack/non-OpenSecrets `external_links` keys are
preserved across re-runs**, keyed by `bioguide_id`. So a quarterly
refresh updates names / parties / committees-of-record without losing
reviewer commentary.

Schema matches `data/politician_profiles.csv` exactly so
`scripts/seed_politician_profiles.py` picks the output up unchanged.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from typing import Any

GOVTRACK_BASE = "https://www.govtrack.us/api/v2"
DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "politician_profiles.csv",
)

CSV_FIELDS = [
    "bioguide_id",
    "name",
    "party",
    "state",
    "chamber",
    "committees",
    "top_industries_current_cycle",
    "interest_group_ratings",
    "external_links",
    "notes",
]

# Auto-discoverable external_links keys we (re)write from GovTrack data.
# Anything else (ballotpedia, wikipedia, custom keys) is preserved as-is
# from any existing CSV row when bioguide_id matches.
AUTO_LINK_KEYS = {"govtrack", "opensecrets"}


def _http_get_json(url: str) -> dict[str, Any]:
    """GET + JSON parse, with a polite User-Agent."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "sift-civic-literacy/1.0 (contact: kristenmartino on GitHub)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body)


def fetch_all_current_roles() -> list[dict[str, Any]]:
    """Paginate /role?current=true and return every role record."""
    roles: list[dict[str, Any]] = []
    offset = 0
    limit = 200
    total = None
    while True:
        url = f"{GOVTRACK_BASE}/role?current=true&limit={limit}&offset={offset}"
        print(f"  GET {url}")
        data = _http_get_json(url)
        roles.extend(data.get("objects", []))
        if total is None:
            total = int(data.get("meta", {}).get("total_count", 0))
        offset += limit
        if offset >= total:
            break
        time.sleep(0.25)  # be polite
    print(f"  Fetched {len(roles)}/{total} role records.")
    return roles


def _normalize_party(p: str | None) -> str:
    if not p:
        return ""
    p = p.strip()
    if p == "Democrat":
        return "D"
    if p == "Republican":
        return "R"
    if p == "Independent":
        return "I"
    return p  # forward-compat for L/G/DFL/etc.


def _normalize_chamber(role_type: str | None) -> str:
    if role_type == "senator":
        return "senate"
    if role_type == "representative":
        return "house"
    return ""


def _build_clean_name(person: dict[str, Any]) -> str:
    """Construct a colloquial display name without title or party-state.

    Prefer the nickname when present (so we get "Bernie Sanders" not
    "Bernard Sanders"), otherwise firstname + lastname. Skip middlename
    by default — the existing curated CSV is a mix of "Charles E. Schumer"
    (with) and "Mitch McConnell" / "Lisa Murkowski" (without), so the
    style isn't strict; the simpler default is easier to skim.
    """
    nickname = (person.get("nickname") or "").strip()
    firstname = (person.get("firstname") or "").strip()
    lastname = (person.get("lastname") or "").strip()
    given = nickname or firstname
    parts = [p for p in (given, lastname) if p]
    return " ".join(parts)


def _build_external_links(role: dict[str, Any]) -> dict[str, str]:
    """Auto-derived links from GovTrack: govtrack person page + OpenSecrets summary."""
    person = role.get("person") or {}
    out: dict[str, str] = {}
    link = (person.get("link") or "").strip()
    if link:
        out["govtrack"] = link
    osid = (person.get("osid") or "").strip()
    if osid:
        out["opensecrets"] = (
            f"https://www.opensecrets.org/members-of-congress/summary?cid={osid}"
        )
    return out


def _read_existing_csv(path: str) -> dict[str, dict[str, str]]:
    """Read existing politician_profiles.csv into {bioguide_id: row} for merge."""
    if not os.path.exists(path):
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            bid = (r.get("bioguide_id") or "").strip()
            if bid:
                out[bid] = r
    return out


def _merge_external_links(
    auto: dict[str, str], existing_json: str | None,
) -> dict[str, str]:
    """Merge auto-derived links with any non-auto keys from the existing row.

    Anything in AUTO_LINK_KEYS gets fresh data from GovTrack (so a
    person-page URL change is reflected on next scrape). Custom keys
    (ballotpedia, wikipedia, official, etc.) survive untouched.
    """
    merged: dict[str, str] = dict(auto)
    if not existing_json:
        return merged
    try:
        existing = json.loads(existing_json)
    except (json.JSONDecodeError, TypeError):
        return merged
    if not isinstance(existing, dict):
        return merged
    for key, value in existing.items():
        if key in AUTO_LINK_KEYS:
            continue
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()
    return merged


def role_to_csv_row(
    role: dict[str, Any], existing: dict[str, str] | None,
) -> dict[str, str] | None:
    """Map a GovTrack role to our CSV row. None when the role isn't usable."""
    person = role.get("person") or {}
    bioguide = (person.get("bioguideid") or "").strip()
    if not bioguide:
        return None
    chamber = _normalize_chamber(role.get("role_type"))
    if not chamber:
        # Skip non-Congress roles (delegates, commissioners, etc.)
        return None

    name = _build_clean_name(person)
    if not name:
        return None

    auto_links = _build_external_links(role)
    merged_links = _merge_external_links(
        auto_links, (existing or {}).get("external_links"),
    )

    row = {
        "bioguide_id": bioguide,
        "name": name,
        "party": _normalize_party(role.get("party")),
        "state": (role.get("state") or "").strip(),
        "chamber": chamber,
        # Hand-curated fields — preserve from existing CSV when present.
        "committees": (existing or {}).get("committees", "").strip() or "[]",
        "top_industries_current_cycle": (
            (existing or {}).get("top_industries_current_cycle", "").strip() or "[]"
        ),
        "interest_group_ratings": (
            (existing or {}).get("interest_group_ratings", "").strip() or "{}"
        ),
        "external_links": json.dumps(merged_links, separators=(", ", ": ")),
        "notes": (existing or {}).get("notes", "").strip(),
    }
    return row


def _sort_key(row: dict[str, str]) -> tuple:
    # senate before house, then state alpha, then lastname (last whitespace-token of name).
    chamber_rank = {"senate": 0, "house": 1}.get(row["chamber"], 9)
    last = row["name"].split()[-1] if row["name"] else ""
    return (chamber_rank, row["state"], last.lower(), row["bioguide_id"])


def main(output: str, limit_pages: int | None) -> None:
    print(f"GovTrack scrape → {output}")
    print("Reading existing CSV (if present) for merge…")
    existing = _read_existing_csv(output)
    print(f"  {len(existing)} existing rows will be merged on bioguide_id match.")

    print("Fetching roles from GovTrack…")
    roles = fetch_all_current_roles()
    if limit_pages is not None:
        # Used during local sanity checking; cap rows for fast iteration.
        roles = roles[: limit_pages * 200]

    rows: list[dict[str, str]] = []
    skipped = 0
    for role in roles:
        bioguide = (role.get("person") or {}).get("bioguideid", "")
        csv_row = role_to_csv_row(role, existing.get(bioguide))
        if csv_row is None:
            skipped += 1
            continue
        rows.append(csv_row)

    rows.sort(key=_sort_key)

    by_chamber: dict[str, int] = {}
    by_party: dict[str, int] = {}
    for r in rows:
        by_chamber[r["chamber"]] = by_chamber.get(r["chamber"], 0) + 1
        by_party[r["party"]] = by_party.get(r["party"], 0) + 1
    print(f"\nResolved {len(rows)} usable roles ({skipped} skipped):")
    print(f"  By chamber: {dict(sorted(by_chamber.items()))}")
    print(f"  By party:   {dict(sorted(by_party.items()))}")

    preserved = sum(
        1 for r in rows
        if r["bioguide_id"] in existing and (
            existing[r["bioguide_id"]].get("notes", "").strip()
            or existing[r["bioguide_id"]].get("committees", "[]").strip() not in ("[]", "")
        )
    )
    print(f"  Preserved from existing CSV: {preserved} rows had notes/committees retained.")

    print(f"\nWriting {output}…")
    with open(output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Done. {len(rows)} rows written.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape GovTrack for all current Congress members → politician_profiles.csv",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"CSV path to write (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--limit-pages", type=int, default=None,
        help="Cap how many pages of roles to fetch (debug only). One page = 200 records.",
    )
    args = parser.parse_args()

    try:
        main(args.output, args.limit_pages)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
