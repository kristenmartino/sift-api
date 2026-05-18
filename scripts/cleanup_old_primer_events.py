"""Enforce 90-day retention on primer_expand_events.

Sister to scripts/cleanup_old_search_queries.py — same shape, same
retention promise, different table. Both honor the 90-day commitment
on /privacy.

Run from sift-api root:

    railway run ./.venv/bin/python3 scripts/cleanup_old_primer_events.py
    railway run ./.venv/bin/python3 scripts/cleanup_old_primer_events.py --dry-run
    railway run ./.venv/bin/python3 scripts/cleanup_old_primer_events.py --days 60

Re-run-safe. Reports rows deleted and current table size.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

DEFAULT_RETENTION_DAYS = 90


async def main(days: int, dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    ssl = "require" if "neon.tech" in db_url else False
    conn = await asyncpg.connect(db_url, ssl=ssl)
    try:
        total_before = await conn.fetchval("SELECT count(*) FROM primer_expand_events")
        eligible = await conn.fetchval(
            "SELECT count(*) FROM primer_expand_events "
            "WHERE created_at < now() - ($1 || ' days')::interval",
            str(days),
        )
        print(f"Rows in primer_expand_events: {total_before:,}")
        print(f"Rows older than {days} days:   {eligible:,}")

        if dry_run:
            print("--dry-run set; no DELETE.")
            return

        if eligible == 0:
            print("Nothing to delete.")
            return

        result = await conn.execute(
            "DELETE FROM primer_expand_events "
            "WHERE created_at < now() - ($1 || ' days')::interval",
            str(days),
        )
        deleted = int(result.rsplit(" ", 1)[1]) if " " in result else 0
        total_after = await conn.fetchval("SELECT count(*) FROM primer_expand_events")
        print(f"Deleted: {deleted:,}")
        print(f"Rows remaining: {total_after:,}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Delete primer_expand_events rows older than --days (default 90).",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_RETENTION_DAYS,
        help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        asyncio.run(main(days=args.days, dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
