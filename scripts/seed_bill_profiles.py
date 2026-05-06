"""Seed bill_profiles from data/bill_profiles.csv.

Run from sift-api root:
    ./.venv/bin/python3 scripts/seed_bill_profiles.py
    railway run ./.venv/bin/python3 scripts/seed_bill_profiles.py  # for prod

Reads data/bill_profiles.csv and upserts each row into bill_profiles.
Idempotent — re-running picks up edits to the CSV (e.g., status changes,
refreshed lobbying numbers, new cosponsors).

CSV column conventions:
  - JSONB columns (cosponsors, external_links): stringified JSON. Empty
    arrays/objects → "[]" / "{}".
  - introduced_date: ISO yyyy-mm-dd or empty.
  - lobbying_for_usd / lobbying_against_usd: numbers (no commas, no $)
    or empty. NULL is acceptable; UI tolerates absence.
  - sponsor_bioguide: must reference an existing politician_profiles
    row, or be empty (FK is ON DELETE SET NULL but on insert it's
    enforced — we set it to NULL when the bioguide isn't yet curated).

Phase 3.A initial seed is small (1 example bill) to exercise the
pipeline. Phase 3.F will populate bills on-demand when articles
reference a bill not yet in the table.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings


CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "bill_profiles.csv",
)

STATUS_VALUES = {
    "introduced", "committee", "passed-chamber", "enacted", "vetoed", "failed",
}


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _parse_int(value: str | None) -> int | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        print(f"  WARN: invalid int '{value}' — storing NULL")
        return None


def _parse_date(value: str | None) -> date | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        print(f"  WARN: invalid ISO date '{value}' — storing NULL")
        return None


def _parse_numeric(value: str | None) -> float | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    try:
        return float(v.replace(",", "").replace("$", ""))
    except ValueError:
        print(f"  WARN: invalid numeric '{value}' — storing NULL")
        return None


def _parse_jsonb(value: str | None, default: str) -> str:
    v = _empty_to_none(value)
    if v is None:
        return default
    try:
        json.loads(v)
        return v
    except json.JSONDecodeError:
        print(f"  WARN: invalid JSON in CSV cell '{value!r}' — using default {default!r}")
        return default


def _validate_status(value: str | None, bill_id: str) -> str | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    if v.lower() not in STATUS_VALUES:
        print(
            f"  WARN: {bill_id}: 'status={v}' not in allowed values "
            f"{sorted(STATUS_VALUES)} — storing as-is."
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
            bill_id = _empty_to_none(raw.get("bill_id"))
            if not bill_id:
                continue
            rows.append(raw)

    print(f"Loaded {len(rows)} bill profiles from {CSV_PATH}")
    by_status: dict[str, int] = {}
    for r in rows:
        s = (r.get("status") or "").strip().lower() or "?"
        by_status[s] = by_status.get(s, 0) + 1
    print(f"  By status: {dict(sorted(by_status.items()))}")

    if dry_run:
        print("--dry-run set; no DB writes.")
        return

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    # Resolve sponsor_bioguide → NULL when the bioguide isn't yet in
    # politician_profiles. Saves the FK violation; the on-demand fetch in
    # Phase 3.F will backfill the link when the politician is curated.
    valid_bioguides = {
        r["bioguide_id"]
        for r in await pool.fetch("SELECT bioguide_id FROM politician_profiles")
    }

    inserted = 0
    updated = 0
    sponsor_dropped = 0

    try:
        for raw in rows:
            bill_id = raw["bill_id"].strip()
            congress = _parse_int(raw.get("congress"))
            if congress is None:
                print(f"  SKIP {bill_id}: congress is required")
                continue

            sponsor = _empty_to_none(raw.get("sponsor_bioguide"))
            if sponsor and sponsor not in valid_bioguides:
                sponsor_dropped += 1
                sponsor = None

            row = {
                "bill_id": bill_id,
                "congress": congress,
                "title": (raw.get("title") or "").strip(),
                "short_title": _empty_to_none(raw.get("short_title")),
                "sponsor_bioguide": sponsor,
                "cosponsors": _parse_jsonb(raw.get("cosponsors"), "[]"),
                "status": _validate_status(raw.get("status"), bill_id),
                "introduced_date": _parse_date(raw.get("introduced_date")),
                "lobbying_for_usd": _parse_numeric(raw.get("lobbying_for_usd")),
                "lobbying_against_usd": _parse_numeric(raw.get("lobbying_against_usd")),
                "external_links": _parse_jsonb(raw.get("external_links"), "{}"),
                "notes": _empty_to_none(raw.get("notes")),
            }

            if not row["title"]:
                print(f"  SKIP {bill_id}: title is empty")
                continue

            existing = await pool.fetchval(
                "SELECT 1 FROM bill_profiles WHERE bill_id = $1", bill_id,
            )

            await pool.execute(
                """
                INSERT INTO bill_profiles
                    (bill_id, congress, title, short_title, sponsor_bioguide,
                     cosponsors, status, introduced_date,
                     lobbying_for_usd, lobbying_against_usd,
                     external_links, notes, updated_at)
                VALUES
                    ($1, $2, $3, $4, $5,
                     $6::jsonb, $7, $8,
                     $9, $10,
                     $11::jsonb, $12, NOW())
                ON CONFLICT (bill_id) DO UPDATE SET
                    congress             = EXCLUDED.congress,
                    title                = EXCLUDED.title,
                    short_title          = EXCLUDED.short_title,
                    sponsor_bioguide     = EXCLUDED.sponsor_bioguide,
                    cosponsors           = EXCLUDED.cosponsors,
                    status               = EXCLUDED.status,
                    introduced_date      = EXCLUDED.introduced_date,
                    lobbying_for_usd     = EXCLUDED.lobbying_for_usd,
                    lobbying_against_usd = EXCLUDED.lobbying_against_usd,
                    external_links       = EXCLUDED.external_links,
                    notes                = EXCLUDED.notes,
                    updated_at           = NOW()
                """,
                row["bill_id"], row["congress"], row["title"], row["short_title"],
                row["sponsor_bioguide"], row["cosponsors"], row["status"],
                row["introduced_date"], row["lobbying_for_usd"],
                row["lobbying_against_usd"], row["external_links"], row["notes"],
            )
            if existing:
                updated += 1
            else:
                inserted += 1

        if sponsor_dropped:
            print(
                f"  Note: {sponsor_dropped} sponsor_bioguide reference(s) NULLed "
                "because the politician isn't yet curated. They'll backfill "
                "when seed_politician_profiles runs with those entries."
            )
        print(f"\nDone! Inserted {inserted} new bills; updated {updated} existing.")

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed bill_profiles from CSV.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + validate the CSV without writing to DB.",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
