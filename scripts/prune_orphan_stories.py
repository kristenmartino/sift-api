"""Delete `stories` rows that no article points at.

WHY THEY EXIST
--------------
`workflows/story_workflow.py` derived `story_id` from a sha256 of its member
article ids, so any membership change minted a *new* row and orphaned the old
one, while `UPDATE articles SET story_id = NULL` cleared the window at the top
of every run. Measured 2026-08-05: **58,259 of 58,557 rows (99.5%) had no
members.** Incremental threading (#158, #161, #180) replaced that with a
stable id derived once from the seed, and the marginal orphan rate is now 0%.

**Order matters and it is already satisfied.** Pruning before the cutover would
have refilled within hours. `docs/NEON_RETENTION.md` says do threading first;
threading went live 2026-08-10 17:39Z and orphan creation stopped.

WHY THIS IS SAFER THAN IT LOOKS
-------------------------------
`articles_story_id_fkey` is `FOREIGN KEY (story_id) REFERENCES stories(id)`
with no `ON DELETE` clause, so it defaults to NO ACTION: **Postgres will
refuse to delete any story an article still points at.** That is a hard
backstop underneath the WHERE clause here — a bug in this script cannot orphan
articles or cascade into them, it can only fail loudly.

The 48-hour floor is the second guard. Threading only ever looks back
`RECENCY_WINDOW_HOURS`, so a story older than that cannot gain a member; one
inside the window might still be mid-assignment. Nothing recent is touched.

The archive is the third. Deleting is irreversible, so the default apply path
writes every row to JSONL first and refuses to proceed if that fails.

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/prune_orphan_stories.py                  # dry run
    ./.venv/bin/python3 scripts/prune_orphan_stories.py --apply
    ./.venv/bin/python3 scripts/prune_orphan_stories.py --apply --older-than 168

Idempotent. Re-running finds only what has orphaned since.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402

# Threading's own lookback. A story older than this cannot gain a member, so it
# is safe to consider settled. Below it, an article may still be on its way.
MIN_AGE_HOURS = 48

CHUNK = 2000
DEFAULT_ARCHIVE = "data/_cache/orphan_stories"


async def main(hours: int, apply: bool, archive_dir: str, chunk: int) -> int:
    if hours < MIN_AGE_HOURS:
        print(f"REFUSING: --older-than {hours} is inside threading's own "
              f"{MIN_AGE_HOURS}h window. A story that recent may still be "
              f"mid-assignment. Raise the floor in this script if you truly mean it.",
              file=sys.stderr)
        return 1

    db_url = settings.database_url
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        before_db = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))")
        before_tbl = await conn.fetchval(
            "SELECT pg_size_pretty(pg_total_relation_size('stories'))")

        where = f"""
            NOT EXISTS (SELECT 1 FROM articles a WHERE a.story_id = s.id)
            AND s.updated_at < NOW() - INTERVAL '{hours} hours'
        """
        total = await conn.fetchval(f"SELECT count(*) FROM stories s WHERE {where}")
        kept = await conn.fetchval("SELECT count(*) FROM stories")

        print(f"database {before_db}   stories table {before_tbl}   {kept:,} rows")
        print(f"orphaned and older than {hours}h: {total:,}")

        # Anything orphaned but too recent to touch — reported so the number is
        # visible rather than silently excluded.
        recent = await conn.fetchval(
            f"""SELECT count(*) FROM stories s
                WHERE NOT EXISTS (SELECT 1 FROM articles a WHERE a.story_id = s.id)
                  AND s.updated_at >= NOW() - INTERVAL '{hours} hours'""")
        print(f"orphaned but within {hours}h (left alone): {recent:,}")

        if total == 0:
            print("\nNothing to prune.")
            return 0

        if not apply:
            print("\nDRY RUN — nothing deleted. Re-run with --apply.")
            print(f"Would archive to {archive_dir}/ then delete {total:,} rows.")
            return 0

        # Archive first. Irreversible deletes get a copy on disk, and a failure
        # to write one aborts rather than proceeding.
        os.makedirs(archive_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(archive_dir, f"orphan_stories.{stamp}.jsonl")
        written = 0
        with open(path, "w", encoding="utf-8") as fh:
            async with conn.transaction():
                async for row in conn.cursor(
                    f"SELECT s.* FROM stories s WHERE {where}"
                ):
                    d = dict(row)
                    for k, v in d.items():
                        if isinstance(v, datetime):
                            d[k] = v.isoformat()
                    fh.write(json.dumps(d, default=str) + "\n")
                    written += 1
        size_mb = os.path.getsize(path) / 1e6
        print(f"\narchived {written:,} rows to {path} ({size_mb:.1f} MB)")
        if written != total:
            print(f"ABORTING: archived {written:,} but expected {total:,}.",
                  file=sys.stderr)
            return 1

        deleted = 0
        while True:
            status = await conn.execute(f"""
                DELETE FROM stories WHERE id IN (
                    SELECT s.id FROM stories s WHERE {where} LIMIT {chunk}
                )
            """)
            n = int(status.rsplit(" ", 1)[-1])
            if n == 0:
                break
            deleted += n
            print(f"  deleted {deleted:,} / {total:,}")

        after_db = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))")
        print(f"\ndeleted {deleted:,} orphaned stories. database {before_db} -> {after_db}")
        print("\nSpace is not returned yet — the rows are dead, not gone. VACUUM marks")
        print("it reusable; only a rewrite gives it back. On 2026-08-05 a plain VACUUM")
        print("reclaimed nothing in 127s while REINDEX returned 491 MB in 42s:")
        print("  VACUUM (ANALYZE) stories;")
        print("  REINDEX TABLE CONCURRENTLY stories;")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--older-than", type=int, default=MIN_AGE_HOURS, dest="hours",
                   help=f"only prune orphans older than N hours (default {MIN_AGE_HOURS})")
    p.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    p.add_argument("--archive", default=DEFAULT_ARCHIVE, help="directory for the JSONL archive")
    p.add_argument("--chunk", type=int, default=CHUNK)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.hours, args.apply, args.archive, args.chunk)))
