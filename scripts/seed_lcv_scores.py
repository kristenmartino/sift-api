"""Seed LCV scorecard entries into politician_profiles.interest_group_ratings.

Run from sift-api root:
    railway run ./.venv/bin/python3 scripts/seed_lcv_scores.py --dry-run
    railway run ./.venv/bin/python3 scripts/seed_lcv_scores.py

Input is data/lcv_scores.csv from scripts/scrape_lcv_scorecard.py.

**One rater, named as such.** LCV is an advocacy group, not a neutral referee.
Each entry stores their own published number with the year and their per-member
URL, and the UI attributes it to them — the treatment outlet_profiles gives
AllSides and MBFC. A conservative counterpart was attempted and is not
obtainable (see scrape_lcv_scorecard.py). Until one is, nothing here may be
presented as a general "rating".

Matching LCV rows to bioguide ids, in three tiers, stopping at the first that
resolves to exactly one member of the right state:

  1. token-equal names, diacritics folded  — "Murkowski, Lisa" == "Lisa Murkowski"
  2. one token set contained in the other  — handles a dropped middle name
  3. surname + state + chamber, unique     — LCV writes "Murphy, Chris" where
     the roster says "Christopher Murphy". A surname is decisive inside one
     state delegation *only* when exactly one member carries it; two Murphys
     in a state means no match rather than a guess.

Anything still unresolved is reported and skipped. Measured 2026-08-07:
532 of 542 matched. The 10 that did not are covered in the run output and
split two ways — members who left Congress mid-cycle (correctly absent from a
current-members roster) and members still serving who are missing from ours
(a roster-freshness problem, not a matching one). Neither is guessable.

Idempotent: replaces any existing LCV entry on a row, leaves other raters'
entries alone, and writes only when the value actually changes.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import unicodedata

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings

RATER = "LCV"
RATER_NAME = "League of Conservation Voters"

STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC", "American Samoa": "AS", "Guam": "GU",
    "Northern Mariana Islands": "MP", "Puerto Rico": "PR",
    "U.S. Virgin Islands": "VI", "Virgin Islands": "VI",
}

_PUNCT = re.compile(r"[^\w\s]")
_NOISE = frozenset({"jr", "sr", "ii", "iii", "iv", "dr", "mrs", "mr", "ms"})


def fold(s: str) -> str:
    """Strip diacritics. LCV writes 'Sánchez'; the bioguide roster writes
    'Sanchez'. Same person, different bytes — four rows turned on this."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def toks(s: str) -> frozenset[str]:
    return frozenset(
        w for w in _PUNCT.sub(" ", fold(s)).lower().split()
        if len(w) > 1 and w not in _NOISE
    )


def match_row(row: dict, by_state: dict[str, list]) -> tuple[str | None, str]:
    """Return (bioguide_id, tier) or (None, reason)."""
    code = STATE_CODES.get(row["state"], "")
    if not code:
        return None, f"unknown state {row['state']!r}"
    cand = by_state.get(code, [])
    if not cand:
        return None, f"no roster rows for {code}"

    want = toks(row["lcv_name"])
    exact = [c for c in cand if toks(c["name"]) == want]
    if len(exact) == 1:
        return exact[0]["bioguide_id"], "exact"

    subset = [
        c for c in cand
        if want and (want <= toks(c["name"]) or toks(c["name"]) <= want)
    ]
    if len(subset) == 1:
        return subset[0]["bioguide_id"], "subset"

    surname = fold(row["lcv_name"].split(",")[0]).strip().lower()
    same_chamber = [
        c for c in cand
        if c["chamber"] == row["chamber"] and surname in toks(c["name"])
    ]
    if len(same_chamber) == 1:
        return same_chamber[0]["bioguide_id"], "surname"
    if len(same_chamber) > 1:
        return None, f"ambiguous surname {surname!r} in {code}"
    return None, "not in roster"


def build_entry(row: dict) -> dict:
    entry = {
        "rater": RATER,
        "rater_name": RATER_NAME,
        "score": int(row["score"]),
        "unit": "percent",
        "year": int(row["year"]),
        "source_url": row["source_url"],
    }
    if str(row.get("lifetime_score", "")).strip():
        entry["lifetime_score"] = int(row["lifetime_score"])
    return entry


def merge(existing: list, entry: dict) -> list:
    """Replace this rater's entry, preserve every other rater's."""
    kept = [
        e for e in existing
        if isinstance(e, dict) and e.get("rater") != RATER
    ]
    return sorted(kept + [entry], key=lambda e: str(e.get("rater", "")))


async def main(csv_path: str, dry_run: bool) -> int:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{csv_path} has no rows.")
        return 1

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl)

    try:
        profs = await pool.fetch(
            "SELECT bioguide_id, name, state, chamber, "
            "       interest_group_ratings::text AS igr "
            "FROM politician_profiles WHERE chamber IN ('house', 'senate')"
        )
        by_state: dict[str, list] = {}
        for r in profs:
            by_state.setdefault((r["state"] or "").upper(), []).append(r)
        current = {r["bioguide_id"]: r["igr"] for r in profs}

        writes: list[tuple[str, str]] = []
        tiers: dict[str, int] = {}
        unmatched: list[tuple[str, str, str]] = []
        unchanged = 0

        for row in rows:
            bid, why = match_row(row, by_state)
            if not bid:
                unmatched.append((row["lcv_name"], row["state"], why))
                continue
            tiers[why] = tiers.get(why, 0) + 1

            try:
                existing = json.loads(current.get(bid) or "[]")
            except (TypeError, ValueError):
                existing = []
            if not isinstance(existing, list):
                existing = []

            new_val = merge(existing, build_entry(row))
            new_json = json.dumps(new_val, separators=(",", ":"), sort_keys=True)
            old_json = json.dumps(
                existing if isinstance(existing, list) else [],
                separators=(",", ":"), sort_keys=True,
            )
            if new_json == old_json:
                unchanged += 1
                continue
            writes.append((new_json, bid))

        print(f"LCV rows:            {len(rows)}")
        print(f"  matched:           {sum(tiers.values())}  {tiers}")
        print(f"  unmatched:         {len(unmatched)}")
        print(f"  to write:          {len(writes)}")
        print(f"  already current:   {unchanged}")

        if unmatched:
            print("\nUnmatched — reported, never guessed:")
            for name, state, why in unmatched:
                print(f"  {name:<30} {state:<18} {why}")
            print("\n  'not in roster' splits two ways: members who left Congress")
            print("  mid-cycle (correct to skip) and sitting members missing from")
            print("  politician_profiles (re-run scripts/scrape_govtrack.py).")

        if dry_run:
            print("\n--dry-run set; no DB writes.")
            return 0

        if writes:
            async with pool.acquire() as conn, conn.transaction():
                await conn.executemany(
                    "UPDATE politician_profiles "
                    "SET interest_group_ratings = $1::jsonb, updated_at = NOW() "
                    "WHERE bioguide_id = $2",
                    writes,
                )
        print(f"\nWrote {len(writes)} rows.")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed LCV scores onto politicians.")
    ap.add_argument("--input", default="data/lcv_scores.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.input, args.dry_run)))
