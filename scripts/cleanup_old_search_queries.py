"""Enforce 90-day retention on search_queries.

Phase 1 search-funnel instrumentation logs every topic-search request to
the `search_queries` table for analytics. To honor the 90-day retention
posture stated in /privacy, this script deletes rows older than 90 days.

Wire into a daily cron / Railway scheduled job once query volume warrants
auto-cleanup. At MVP volume (<1k rows/day) running once a week is fine.

Run from sift-api root:

    railway run ./.venv/bin/python3 scripts/cleanup_old_search_queries.py
    railway run ./.venv/bin/python3 scripts/cleanup_old_search_queries.py --dry-run
    railway run ./.venv/bin/python3 scripts/cleanup_old_search_queries.py --days 60

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
        total_before = await conn.fetchval("SELECT count(*) FROM search_queries")
        eligible = await conn.fetchval(
            "SELECT count(*) FROM search_queries "
            "WHERE created_at < now() - ($1 || ' days')::interval",
            str(days),
        )
        print(f"Rows in search_queries:        {total_before:,}")
        print(f"Rows older than {days} days:   {eligible:,}")

        if dry_run:
            print("--dry-run set; no DELETE.")
            return

        if eligible == 0:
            print("Nothing to delete.")
            return

        result = await conn.execute(
            "DELETE FROM search_queries "
            "WHERE created_at < now() - ($1 || ' days')::interval",
            str(days),
        )
        # asyncpg returns 'DELETE <n>' as the command tag
        deleted = int(result.rsplit(" ", 1)[1]) if " " in result else 0
        total_after = await conn.fetchval("SELECT count(*) FROM search_queries")
        print(f"Deleted: {deleted:,}")
        print(f"Rows remaining: {total_after:,}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Delete search_queries rows older than --days (default 90).",
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
