"""Seed source_name_aliases from a reviewed suggestions CSV.

Run from sift-api root, after running `audit_source_aliases.py` and
manually reviewing the output:

    railway run ./.venv/bin/python3 scripts/seed_source_aliases.py \\
        --input data/source_alias_suggestions.csv

Reads the CSV produced by `audit_source_aliases.py` (after human review).
Each row maps a `source_name` (raw string from RSS) to an `outlet_slug`
(canonical key in `outlet_profiles`). Rows with empty `suggested_outlet_slug`
are skipped — leave a row empty to opt out of mapping that source_name.

Idempotent — running again with the same CSV is a no-op.

CSV schema (must match `audit_source_aliases.py` output):
    source_name | article_count | suggested_outlet_slug | confidence

`article_count` and `confidence` are informational; only `source_name` and
`suggested_outlet_slug` are persisted.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings


async def main(input_path: str, dry_run: bool) -> None:
    if not os.path.exists(input_path):
        print(f"ERROR: {input_path} not found. Aborting.")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False

    rows: list[tuple[str, str]] = []  # [(source_name, slug)]
    skipped_empty = 0
    with open(input_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            source_name = (r.get("source_name") or "").strip()
            slug = (r.get("suggested_outlet_slug") or "").strip()
            if not source_name:
                continue
            if not slug:
                skipped_empty += 1
                continue
            rows.append((source_name, slug))

    print(f"Loaded {len(rows)} mappings from {input_path}")
    if skipped_empty:
        print(f"  Skipped {skipped_empty} rows with empty suggested_outlet_slug.")

    if dry_run:
        print("--dry-run set; no DB writes.")
        for sn, slug in rows[:20]:
            print(f"  {sn!r:40s} -> {slug}")
        if len(rows) > 20:
            print(f"  … and {len(rows) - 20} more")
        return

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    # Validate slugs against outlet_profiles before writing to fail fast
    # rather than on individual insert.
    valid_slugs = {
        r["slug"]
        for r in await pool.fetch("SELECT slug FROM outlet_profiles")
    }
    if not valid_slugs:
        print("ERROR: outlet_profiles is empty. Run scripts/seed_outlet_profiles.py first.")
        await pool.close()
        sys.exit(1)

    invalid = [(sn, slug) for sn, slug in rows if slug not in valid_slugs]
    if invalid:
        print(f"ERROR: {len(invalid)} mapping(s) reference unknown outlet_slugs:")
        for sn, slug in invalid[:10]:
            print(f"  {sn!r} -> {slug}")
        if len(invalid) > 10:
            print(f"  … and {len(invalid) - 10} more")
        print("Add these slugs to outlet_profiles, or correct the CSV.")
        await pool.close()
        sys.exit(1)

    inserted = 0
    updated = 0
    try:
        for source_name, slug in rows:
            existing = await pool.fetchval(
                "SELECT outlet_slug FROM source_name_aliases WHERE raw_source_name = $1",
                source_name,
            )
            await pool.execute(
                """
                INSERT INTO source_name_aliases (raw_source_name, outlet_slug)
                VALUES ($1, $2)
                ON CONFLICT (raw_source_name) DO UPDATE SET
                    outlet_slug = EXCLUDED.outlet_slug,
                    added_at    = NOW()
                """,
                source_name, slug,
            )
            if existing is None:
                inserted += 1
            elif existing != slug:
                updated += 1

        print(f"\nDone! Inserted {inserted} new aliases; updated {updated} existing mappings.")
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed source_name_aliases from reviewed CSV.")
    parser.add_argument(
        "--input", default="data/source_alias_suggestions.csv",
        help="CSV to read (default: data/source_alias_suggestions.csv).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.input, args.dry_run))
