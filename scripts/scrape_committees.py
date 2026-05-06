"""Enrich politician_profiles with committee assignments.

Phase 3.F.1 — civic-literacy MVP. Fetches the canonical
`committees-current.yaml` and `committee-membership-current.yaml` from
the community-maintained `unitedstates/congress-legislators` repo,
indexes them by bioguide_id, and merges committee names into each row
of `data/politician_profiles.csv`.

Run from sift-api root:
    ./.venv/bin/python3 scripts/scrape_committees.py
    ./.venv/bin/python3 scripts/scrape_committees.py --output data/politician_profiles.csv

Why this source?
----------------
GovTrack v2 doesn't expose committee membership (the legacy
`/committee_membership` endpoint 404s; role objects don't include
committee assignments either). Congress.gov's API requires a key
with rate limits.

`unitedstates/congress-legislators` is the canonical community-maintained
source — used by ProPublica, GovTrack itself, and most third-party
civic-tech tools. It's manually updated within ~24 hours of any
committee membership change.

What it captures
----------------
Top-level committees only (Senate, House, Joint). Subcommittees are
intentionally skipped — they'd 3x the list and add noise to the
dossier without proportional civic-literacy value.

Display normalization
---------------------
Strips the "Senate Committee on " / "House Committee on " /
"Joint Committee on " prefixes since chamber is already on the
politician profile. Result reads like the existing curated samples:
  "Senate Committee on Finance" → "Finance"
  "House Committee on Foreign Affairs" → "Foreign Affairs"

Re-run-safe
-----------
Updates only the `committees` field on matching bioguide_id rows.
Preserves notes, external_links, top_industries_current_cycle,
interest_group_ratings, party, state, chamber, name. Politicians not
in the membership data (members at-large, freshly-elected, sitting in
non-Congressional roles) are left unchanged.

Phase 3.F roadmap
-----------------
3.F.1 (this script): committees from unitedstates/congress-legislators
3.F.2 (queued): OpenSecrets + Vote Smart enrichment (requires API keys)
3.F.3 (queued): daily-refresh cron orchestrator
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request
from typing import Any

import yaml

DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "politician_profiles.csv",
)

UNITEDSTATES_BASE = (
    "https://raw.githubusercontent.com/"
    "unitedstates/congress-legislators/main"
)
COMMITTEES_URL = f"{UNITEDSTATES_BASE}/committees-current.yaml"
MEMBERSHIP_URL = f"{UNITEDSTATES_BASE}/committee-membership-current.yaml"

# Display-form prefix strippers — applied in order, first match wins.
# Order matters: longer/more-specific variants must come before shorter ones,
# otherwise "Senate Committee on " would steal a match from "Senate Select
# Committee on " before the latter could fire.
PREFIXES_TO_STRIP = [
    "United States Senate Committee on the ",
    "United States Senate Committee on ",
    "United States House Committee on the ",
    "United States House Committee on ",
    "Senate Committee on the ",
    "Senate Committee on ",
    "House Committee on the ",
    "House Committee on ",
    "Joint Committee of Congress on the ",
    "Joint Committee of Congress on ",
    "Joint Committee on the ",
    "Joint Committee on ",
]

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


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "sift-civic-literacy/1.0 (contact: kristenmartino on GitHub)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _strip_prefix(name: str) -> str:
    """Return the display form of a committee name (no chamber prefix).

    "Senate Committee on the Judiciary" → "Judiciary"
    "Senate Committee on Finance"      → "Finance"
    "Senate Select Committee on Intelligence" → "Senate Select Committee on Intelligence"
      (Select / Special committees keep their full name — the prefix is
      editorially meaningful, not redundant chamber boilerplate.)
    """
    n = name.strip()
    for prefix in PREFIXES_TO_STRIP:
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
            break
    # Defensive: strip a leading "the " if a "Senate Committee on the X" form
    # slipped through with the prefix-stripper picking the shorter variant
    # first. (Belt-and-suspenders given the long-prefixes-first ordering above.)
    if n.lower().startswith("the "):
        n = n[4:].strip()
    return n


def _load_committees_yaml() -> list[dict[str, Any]]:
    print(f"  GET {COMMITTEES_URL}")
    body = _http_get(COMMITTEES_URL)
    data = yaml.safe_load(body)
    if not isinstance(data, list):
        raise RuntimeError(
            "committees-current.yaml: expected a list at the root, got "
            f"{type(data).__name__}",
        )
    return data


def _load_membership_yaml() -> dict[str, list[dict[str, Any]]]:
    print(f"  GET {MEMBERSHIP_URL}")
    body = _http_get(MEMBERSHIP_URL)
    data = yaml.safe_load(body)
    if not isinstance(data, dict):
        raise RuntimeError(
            "committee-membership-current.yaml: expected a dict at the root, "
            f"got {type(data).__name__}",
        )
    return data


def build_bioguide_to_committees(
    committees: list[dict[str, Any]],
    membership: dict[str, list[dict[str, Any]]],
) -> dict[str, list[str]]:
    """Index {bioguide_id → [display committee name, ...]}.

    Top-level committees only. Subcommittees are skipped to keep the
    dossier list short and editorially relevant.
    """
    # 1. Build {thomas_id → display name} for top-level committees.
    name_by_thomas_id: dict[str, str] = {}
    for committee in committees:
        thomas_id = committee.get("thomas_id")
        name = committee.get("name")
        if not thomas_id or not name:
            continue
        name_by_thomas_id[str(thomas_id)] = _strip_prefix(str(name))

    # 2. Walk membership, collecting per-bioguide assignments. Skip any
    #    membership keys that don't correspond to a top-level committee
    #    (subcommittees have keys like "SSGA01" — parent + digit suffix).
    out: dict[str, list[str]] = {}
    for thomas_id, members in membership.items():
        if thomas_id not in name_by_thomas_id:
            continue
        committee_name = name_by_thomas_id[thomas_id]
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            bioguide = member.get("bioguide")
            if not isinstance(bioguide, str) or not bioguide.strip():
                continue
            bioguide = bioguide.strip()
            out.setdefault(bioguide, [])
            if committee_name not in out[bioguide]:
                out[bioguide].append(committee_name)

    # Stable sort within each member's list so re-runs produce identical CSVs.
    for bioguide in out:
        out[bioguide].sort()

    return out


def _read_existing_csv(path: str) -> tuple[list[str], list[dict[str, str]]]:
    """Return (fieldnames, rows). Falls back to CSV_FIELDS for fieldnames if file is missing."""
    if not os.path.exists(path):
        return CSV_FIELDS, []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or CSV_FIELDS
        return list(fieldnames), list(reader)


def main(output: str) -> None:
    print(f"Committee scrape → {output}")
    print("Fetching unitedstates/congress-legislators YAMLs…")
    committees_data = _load_committees_yaml()
    membership_data = _load_membership_yaml()
    index = build_bioguide_to_committees(committees_data, membership_data)
    print(
        f"  Indexed {len(index)} bioguides across "
        f"{sum(len(v) for v in index.values())} committee assignments."
    )

    fieldnames, rows = _read_existing_csv(output)
    if not rows:
        print(f"  WARN: {output} has no rows; nothing to update. "
              "Run scrape_govtrack.py first to seed the politician list.")
        return

    updated = 0
    skipped_no_match = 0
    for row in rows:
        bid = (row.get("bioguide_id") or "").strip()
        if not bid:
            continue
        new_committees = index.get(bid)
        if not new_committees:
            skipped_no_match += 1
            continue
        # Only overwrite when the new list actually differs — keeps the
        # diff minimal on re-runs.
        new_json = json.dumps(new_committees)
        if (row.get("committees") or "").strip() == new_json:
            continue
        row["committees"] = new_json
        updated += 1

    print(
        f"\nUpdated committees for {updated} politicians. "
        f"{skipped_no_match} bioguides had no committee assignments "
        "(at-large, freshly-elected, or non-Congress roles)."
    )

    with open(output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {output}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Enrich politician_profiles.csv with committee assignments from "
            "unitedstates/congress-legislators."
        ),
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"CSV path to update (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()

    try:
        main(args.output)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
