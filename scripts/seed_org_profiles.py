"""Seed org_profiles from data/org_profiles.csv.

Run from sift-api root:
    ./.venv/bin/python3 scripts/seed_org_profiles.py
    railway run ./.venv/bin/python3 scripts/seed_org_profiles.py  # for prod

Reads data/org_profiles.csv and upserts each row into org_profiles.
Idempotent — re-running picks up edits to the CSV (e.g., refreshed 990
data, new FARA registrations, founder updates).

CSV column conventions:
  - JSONB columns (major_funders, fara_countries, external_links):
    stringified JSON in the CSV cell. Empty arrays/objects → "[]" / "{}".
  - founded_year: integer or empty.
  - annual_budget_usd: number (no commas, no $ sign) or empty.
  - fara_registered: 'true' / 'false' / '' (empty).
  - Empty strings normalize to NULL on insert (NUMERIC nullable;
    BOOLEAN defaults FALSE in schema).

Phase 3.A initial seed is small (~10 example rows spanning the political
spectrum and org-type variety) to exercise the pipeline. Phase 3.D will
grow this to ~200 hand-curated entries before the inline-glossary
feature ships.
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
    "org_profiles.csv",
)

ORG_TYPES = {
    "think-tank", "advocacy", "union", "pac", "super-pac",
    "foundation", "industry-group", "other",
}
LEAN_VALUES = {
    "left", "lean-left", "center", "lean-right", "right",
    "mixed", "nonpartisan",
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


def _parse_numeric(value: str | None) -> float | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    try:
        return float(v.replace(",", "").replace("$", ""))
    except ValueError:
        print(f"  WARN: invalid numeric '{value}' — storing NULL")
        return None


def _parse_bool(value: str | None) -> bool | None:
    v = _empty_to_none(value)
    if v is None:
        return None
    lower = v.lower()
    if lower in {"true", "yes", "1", "t", "y"}:
        return True
    if lower in {"false", "no", "0", "f", "n"}:
        return False
    print(f"  WARN: invalid bool '{value}' — storing FALSE (schema default)")
    return False


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

    print(f"Loaded {len(rows)} org profiles from {CSV_PATH}")
    by_type: dict[str, int] = {}
    by_lean: dict[str, int] = {}
    for r in rows:
        t = (r.get("type") or "").strip().lower() or "?"
        l = (r.get("political_lean") or "").strip().lower() or "?"
        by_type[t] = by_type.get(t, 0) + 1
        by_lean[l] = by_lean.get(l, 0) + 1
    print(f"  By type: {dict(sorted(by_type.items()))}")
    print(f"  By lean: {dict(sorted(by_lean.items()))}")
    fara_count = sum(1 for r in rows if (r.get("fara_registered") or "").strip().lower() in {"true", "yes", "1", "t", "y"})
    print(f"  FARA-registered: {fara_count}/{len(rows)}")

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
                "type": _validate_enum(raw.get("type"), ORG_TYPES, "type", slug),
                "political_lean": _validate_enum(
                    raw.get("political_lean"), LEAN_VALUES, "political_lean", slug,
                ),
                "founded_year": _parse_int(raw.get("founded_year")),
                "annual_budget_usd": _parse_numeric(raw.get("annual_budget_usd")),
                "major_funders": _parse_jsonb(raw.get("major_funders"), "[]"),
                "fara_registered": _parse_bool(raw.get("fara_registered")),
                "fara_countries": _parse_jsonb(raw.get("fara_countries"), "[]"),
                "external_links": _parse_jsonb(raw.get("external_links"), "{}"),
                "notes": _empty_to_none(raw.get("notes")),
            }

            if not row["name"]:
                print(f"  SKIP {slug}: name is empty")
                continue

            existing = await pool.fetchval(
                "SELECT 1 FROM org_profiles WHERE slug = $1", slug,
            )

            await pool.execute(
                """
                INSERT INTO org_profiles
                    (slug, name, type, political_lean, founded_year,
                     annual_budget_usd, major_funders, fara_registered,
                     fara_countries, external_links, notes, updated_at)
                VALUES
                    ($1, $2, $3, $4, $5,
                     $6, $7::jsonb, $8,
                     $9::jsonb, $10::jsonb, $11, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                    name              = EXCLUDED.name,
                    type              = EXCLUDED.type,
                    political_lean    = EXCLUDED.political_lean,
                    founded_year      = EXCLUDED.founded_year,
                    annual_budget_usd = EXCLUDED.annual_budget_usd,
                    major_funders     = EXCLUDED.major_funders,
                    fara_registered   = EXCLUDED.fara_registered,
                    fara_countries    = EXCLUDED.fara_countries,
                    external_links    = EXCLUDED.external_links,
                    notes             = EXCLUDED.notes,
                    updated_at        = NOW()
                """,
                row["slug"], row["name"], row["type"], row["political_lean"],
                row["founded_year"], row["annual_budget_usd"], row["major_funders"],
                row["fara_registered"], row["fara_countries"], row["external_links"],
                row["notes"],
            )
            if existing:
                updated += 1
            else:
                inserted += 1

        print(f"\nDone! Inserted {inserted} new orgs; updated {updated} existing.")

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed org_profiles from CSV.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + validate the CSV without writing to DB.",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
