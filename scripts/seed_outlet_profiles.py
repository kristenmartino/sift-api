"""Seed outlet_profiles from data/outlet_profiles.csv.

Run from sift-api root:
    ./.venv/bin/python3 scripts/seed_outlet_profiles.py
    railway run ./.venv/bin/python3 scripts/seed_outlet_profiles.py  # for prod

Reads data/outlet_profiles.csv and upserts each row into outlet_profiles.
Idempotent — re-running picks up edits to the CSV (e.g., AllSides ratings
filled in after the initial canonical-info seed).

Empty rating columns (allsides_rating, mbfc_factual) are stored as NULL —
the UI tolerates NULL and renders no badge for that source on those rows.

CSV column conventions:
  - JSONB columns (major_funders, external_links): stringified JSON in
    the CSV cell. Empty arrays/objects → "[]" / "{}".
  - DATE columns (allsides_last_checked, mbfc_last_checked): ISO yyyy-mm-dd
    or empty.
  - Empty strings are normalized to NULL on insert.
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
    "outlet_profiles.csv",
)

ALLSIDES_VALUES = {"left", "lean-left", "center", "lean-right", "right", "mixed"}
MBFC_VALUES = {"high", "mostly-factual", "mixed", "low", "very-low"}
FUNDING_VALUES = {
    "subscription", "advertising", "foundation", "donations",
    "mixed", "public-service",
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


def _parse_jsonb(value: str | None, default: str) -> str:
    """Returns a JSON string ready for ::jsonb cast. Empty → default."""
    v = _empty_to_none(value)
    if v is None:
        return default
    try:
        # Validate it's parseable JSON
        json.loads(v)
        return v
    except json.JSONDecodeError:
        print(f"  WARN: invalid JSON in CSV cell '{value!r}' — using default {default!r}")
        return default


def _validate_enum(value: str | None, allowed: set[str], field: str, slug: str) -> str | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    if v.lower() not in allowed:
        print(
            f"  WARN: {slug}: '{field}={v}' not in allowed values {sorted(allowed)} — "
            "storing as-is anyway; UI will treat unknown values as missing."
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
            slug = _empty_to_none(raw.get("slug"))
            if not slug:
                continue
            rows.append(raw)

    print(f"Loaded {len(rows)} outlet profiles from {CSV_PATH}")
    rated = sum(1 for r in rows if _empty_to_none(r.get("allsides_rating")))
    fact_rated = sum(1 for r in rows if _empty_to_none(r.get("mbfc_factual")))
    print(f"  AllSides rating filled: {rated}/{len(rows)}")
    print(f"  MBFC factual filled:    {fact_rated}/{len(rows)}")

    if dry_run:
        print("--dry-run set; no DB writes.")
        return

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)
    inserted = 0
    updated = 0

    try:
        for raw in rows:
            slug = raw["slug"].strip()

            row = {
                "slug": slug,
                "name": (raw.get("name") or "").strip(),
                "parent_company": _empty_to_none(raw.get("parent_company")),
                "parent_company_url": _empty_to_none(raw.get("parent_company_url")),
                "founded_year": _parse_int(raw.get("founded_year")),
                "funding_model": _validate_enum(
                    raw.get("funding_model"), FUNDING_VALUES, "funding_model", slug,
                ),
                "major_funders": _parse_jsonb(raw.get("major_funders"), "[]"),
                "allsides_rating": _validate_enum(
                    raw.get("allsides_rating"), ALLSIDES_VALUES, "allsides_rating", slug,
                ),
                "allsides_url": _empty_to_none(raw.get("allsides_url")),
                "allsides_last_checked": _parse_date(raw.get("allsides_last_checked")),
                "mbfc_factual": _validate_enum(
                    raw.get("mbfc_factual"), MBFC_VALUES, "mbfc_factual", slug,
                ),
                "mbfc_url": _empty_to_none(raw.get("mbfc_url")),
                "mbfc_last_checked": _parse_date(raw.get("mbfc_last_checked")),
                "notes": _empty_to_none(raw.get("notes")),
                "external_links": _parse_jsonb(raw.get("external_links"), "{}"),
            }

            if not row["name"]:
                print(f"  SKIP {slug}: name is empty")
                continue

            existing = await pool.fetchval(
                "SELECT 1 FROM outlet_profiles WHERE slug = $1", slug,
            )

            await pool.execute(
                """
                INSERT INTO outlet_profiles
                    (slug, name, parent_company, parent_company_url, founded_year,
                     funding_model, major_funders, allsides_rating, allsides_url,
                     allsides_last_checked, mbfc_factual, mbfc_url, mbfc_last_checked,
                     notes, external_links, updated_at)
                VALUES
                    ($1, $2, $3, $4, $5,
                     $6, $7::jsonb, $8, $9,
                     $10, $11, $12, $13,
                     $14, $15::jsonb, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                    name                  = EXCLUDED.name,
                    parent_company        = EXCLUDED.parent_company,
                    parent_company_url    = EXCLUDED.parent_company_url,
                    founded_year          = EXCLUDED.founded_year,
                    funding_model         = EXCLUDED.funding_model,
                    major_funders         = EXCLUDED.major_funders,
                    allsides_rating       = EXCLUDED.allsides_rating,
                    allsides_url          = EXCLUDED.allsides_url,
                    allsides_last_checked = EXCLUDED.allsides_last_checked,
                    mbfc_factual          = EXCLUDED.mbfc_factual,
                    mbfc_url              = EXCLUDED.mbfc_url,
                    mbfc_last_checked     = EXCLUDED.mbfc_last_checked,
                    notes                 = EXCLUDED.notes,
                    external_links        = EXCLUDED.external_links,
                    updated_at            = NOW()
                """,
                row["slug"], row["name"], row["parent_company"], row["parent_company_url"],
                row["founded_year"], row["funding_model"], row["major_funders"],
                row["allsides_rating"], row["allsides_url"], row["allsides_last_checked"],
                row["mbfc_factual"], row["mbfc_url"], row["mbfc_last_checked"],
                row["notes"], row["external_links"],
            )
            if existing:
                updated += 1
            else:
                inserted += 1

        print(f"\nDone! Inserted {inserted} new outlets; updated {updated} existing.")

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed outlet_profiles from CSV.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + validate the CSV without writing to DB.",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
