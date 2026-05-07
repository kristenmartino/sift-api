"""Backfill articles.entity_links after the Phase 3.G linker policy change.

Followup to sift-api PR #40 — `politician_aliases` was returning
last-name-only forms that false-matched common English words in news
copy ("downing power lines" → Troy Downing, "the case involves" →
Ed Case, etc.). The PR fixed the policy going forward; existing
`articles.entity_links` JSONB rows still hold the bad chips.

This script re-runs the linker over every article that has a non-empty
`entity_links` value and writes the corrected list back. Articles that
have always been empty are left untouched (the periodic pipeline will
populate them with the new policy as it processes new content).

Idempotent. Safe to re-run; only writes a row when the new value differs
from the existing one.

Usage (from sift-api root):

    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py
    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py --dry-run

Cost: regex-only, no LLM calls. ~50 articles in current prod state
runs in well under a second; safe even if the affected count grows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# scripts/ sits next to services/ — make the latter importable when run
# from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from services.entity_linker import (  # noqa: E402
    build_catalog,
    build_search_dict,
    link_text,
)


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    ssl = "require" if "neon.tech" in db_url else False
    conn = await asyncpg.connect(db_url, ssl=ssl)
    try:
        # Catalog — same four queries the pipeline node uses.
        outlets = [dict(r) for r in await conn.fetch(
            "SELECT slug, name FROM outlet_profiles"
        )]
        politicians = [dict(r) for r in await conn.fetch(
            "SELECT bioguide_id, name FROM politician_profiles"
        )]
        orgs = [dict(r) for r in await conn.fetch(
            "SELECT slug, name FROM org_profiles"
        )]
        bills = [dict(r) for r in await conn.fetch(
            "SELECT bill_id, title, short_title FROM bill_profiles"
        )]
        catalog = build_catalog(outlets, politicians, orgs, bills)
        search_dict = build_search_dict(catalog)
        print(
            f"Catalog: {len(outlets)} outlets, {len(politicians)} politicians, "
            f"{len(orgs)} orgs, {len(bills)} bills → {len(search_dict)} search keys"
        )

        # Pull every article with a non-empty entity_links column. We
        # leave the (much larger) set of always-empty rows alone — the
        # pipeline will populate those for new articles with the corrected
        # policy.
        rows = await conn.fetch(
            """
            SELECT id, title, summary, entity_links::text AS el
            FROM articles
            WHERE entity_links IS NOT NULL
              AND entity_links::text != '[]'
            ORDER BY created_at DESC
            """
        )
        print(f"Articles with non-empty entity_links: {len(rows)}")

        updated = 0
        no_change = 0
        cleared = 0
        for r in rows:
            text = f"{r['title'] or ''}\n{r['summary'] or ''}"
            new_links = link_text(text, search_dict)
            new_json = json.dumps(new_links, separators=(",", ":"))
            old_json = r["el"]
            if old_json == new_json:
                no_change += 1
                continue
            if not new_links:
                cleared += 1
            updated += 1
            if not dry_run:
                await conn.execute(
                    "UPDATE articles SET entity_links = $1::jsonb WHERE id = $2",
                    new_json, r["id"],
                )

        print()
        print(f"  updated:     {updated}")
        print(f"    of which cleared to []: {cleared}")
        print(f"  unchanged:   {no_change}")
        if dry_run:
            print()
            print("--dry-run set; no DB writes.")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-run the entity linker over articles with stored "
                    "entity_links and write the corrected list back.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the diff but don't write to the DB.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
