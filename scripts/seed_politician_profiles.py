"""Seed politician_profiles from data/politician_profiles.csv.

Run from sift-api root:
    ./.venv/bin/python3 scripts/seed_politician_profiles.py
    railway run ./.venv/bin/python3 scripts/seed_politician_profiles.py  # for prod

Reads data/politician_profiles.csv and upserts each row into politician_profiles.
Idempotent — re-running picks up edits to the CSV (e.g., new committee
assignments, party switches, refreshed donor data from OpenSecrets).

CSV column conventions:
  - JSONB columns (committees, top_industries_current_cycle,
    interest_group_ratings, external_links): stringified JSON in the CSV
    cell. Empty arrays/objects → "[]" / "{}".
  - Empty strings normalize to NULL on insert.
  - bioguide_id is the Congress.gov canonical identifier (e.g. 'S000148'
    for Schumer); it's the primary key.

Phase 3.A initial seed is small (~5–10 example rows) to exercise the
pipeline. Phase 3.B will replace it with all 535 sitting members via a
GovTrack scrape.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings


CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "politician_profiles.csv",
)

CHAMBER_VALUES = {"senate", "house", "former", "executive"}


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _parse_jsonb(value: str | None, default: str) -> str:
    """Returns a JSON string ready for ::jsonb cast. Empty → default."""
    v = _empty_to_none(value)
    if v is None:
        return default
    try:
        json.loads(v)
        return v
    except json.JSONDecodeError:
        print(f"  WARN: invalid JSON in CSV cell '{value!r}' — using default {default!r}")
        return default


def _validate_chamber(value: str | None, bioguide_id: str) -> str | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    if v.lower() not in CHAMBER_VALUES:
        print(
            f"  WARN: {bioguide_id}: 'chamber={v}' not in allowed values "
            f"{sorted(CHAMBER_VALUES)} — storing as-is."
        )
    return v.lower()


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False

    if not os.path.exists(CSV_PATH):
        print(f"ERROR: {CSV_PATH} not found. Aborting.")
        sys.exit(1)

    rows: list[dict] = []
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            bioguide = _empty_to_none(raw.get("bioguide_id"))
            if not bioguide:
                continue
            rows.append(raw)

    print(f"Loaded {len(rows)} politician profiles from {CSV_PATH}")
    by_party: dict[str, int] = {}
    by_chamber: dict[str, int] = {}
    for r in rows:
        p = (r.get("party") or "").strip().upper() or "?"
        c = (r.get("chamber") or "").strip().lower() or "?"
        by_party[p] = by_party.get(p, 0) + 1
        by_chamber[c] = by_chamber.get(c, 0) + 1
    print(f"  By party:   {dict(sorted(by_party.items()))}")
    print(f"  By chamber: {dict(sorted(by_chamber.items()))}")

    if dry_run:
        print("--dry-run set; no DB writes.")
        return

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)
    inserted = 0
    updated = 0

    try:
        for raw in rows:
            bioguide = raw["bioguide_id"].strip()

            row = {
                "bioguide_id": bioguide,
                "name": (raw.get("name") or "").strip(),
                "party": _empty_to_none(raw.get("party")),
                "state": _empty_to_none(raw.get("state")),
                "chamber": _validate_chamber(raw.get("chamber"), bioguide),
                "committees": _parse_jsonb(raw.get("committees"), "[]"),
                "top_industries_current_cycle": _parse_jsonb(
                    raw.get("top_industries_current_cycle"), "[]",
                ),
                "interest_group_ratings": _parse_jsonb(
                    raw.get("interest_group_ratings"), "{}",
                ),
                "external_links": _parse_jsonb(raw.get("external_links"), "{}"),
                "notes": _empty_to_none(raw.get("notes")),
            }

            if not row["name"]:
                print(f"  SKIP {bioguide}: name is empty")
                continue

            existing = await pool.fetchval(
                "SELECT 1 FROM politician_profiles WHERE bioguide_id = $1",
                bioguide,
            )

            await pool.execute(
                """
                INSERT INTO politician_profiles
                    (bioguide_id, name, party, state, chamber,
                     committees, top_industries_current_cycle, interest_group_ratings,
                     external_links, notes, updated_at)
                VALUES
                    ($1, $2, $3, $4, $5,
                     $6::jsonb, $7::jsonb, $8::jsonb,
                     $9::jsonb, $10, NOW())
                ON CONFLICT (bioguide_id) DO UPDATE SET
                    name                          = EXCLUDED.name,
                    party                         = EXCLUDED.party,
                    state                         = EXCLUDED.state,
                    chamber                       = EXCLUDED.chamber,
                    committees                    = EXCLUDED.committees,
                    top_industries_current_cycle  = EXCLUDED.top_industries_current_cycle,
                    interest_group_ratings        = EXCLUDED.interest_group_ratings,
                    external_links                = EXCLUDED.external_links,
                    notes                         = EXCLUDED.notes,
                    updated_at                    = NOW()
                """,
                row["bioguide_id"], row["name"], row["party"], row["state"],
                row["chamber"], row["committees"], row["top_industries_current_cycle"],
                row["interest_group_ratings"], row["external_links"], row["notes"],
            )
            if existing:
                updated += 1
            else:
                inserted += 1

        print(f"\nDone! Inserted {inserted} new politicians; updated {updated} existing.")

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed politician_profiles from CSV.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + validate the CSV without writing to DB.",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
