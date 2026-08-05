"""Reclaim Neon storage by NULLing embeddings on articles past a cutoff.

WHY NULL THE COLUMN RATHER THAN DELETE THE ROW
----------------------------------------------
Measured 2026-08-05: the database is 2,272 MB, and **63% of it is one column
and its index** — `articles.embedding` (554 MB, TOASTed) plus
`idx_articles_embedding` (879 MB, 39% of the whole DB). The article text is
comparatively tiny: all 229k pre-30-day rows together carry only ~100 MB of
title + summary + primer.

So deleting rows buys ~100 MB more than NULLing embeddings does, and costs:
`entity_links` and dossier references, `story_id` history, the `source_url` /
`content_hash` rows the deduplicator checks against, and any chance of
reversing the decision. NULLing gives up the same thing deleting does — vector
reach over old articles — for almost all of the same space, and is reversible.

**It is safe because every consumer already guards on the column.** Verified
2026-08-05, all four:

    workflows/story_workflow.py:51      AND embedding IS NOT NULL
    sift/lib/db.ts:349                  WHERE embedding IS NOT NULL   (topic search)
    sift-mcp/server.py:234              WHERE embedding IS NOT NULL   (semantic search)
    sift-mcp/server.py:486              WHERE embedding IS NOT NULL   (compare_outlets)

Each one silently excludes a NULL-embedding row rather than erroring. Nothing
else reads the column. The feed path in `sift/lib/db.ts` filters on
`summary IS NOT NULL`, not on the embedding, so the feed is untouched.

**Reversible:** `backfill_embeddings.py` rebuilds an embedding from
`title + summary`, both of which this script keeps. That is the same repair
path already used for rows whose embedding failed at ingest.

WHAT YOU ACTUALLY GIVE UP
-------------------------
Vector-search reach over articles older than the cutoff. Two surfaces use it:
topic search in `sift`, and semantic search / `compare_outlets` in `sift-mcp`.
Neither has a date floor, so today they search the entire corpus.

Weigh that against observed use: `search_queries` holds **6 rows for the
product's entire history**, the newest 2026-07-14. The capability is real; the
usage is not. Decide with that in front of you, not the storage number alone.

Space is NOT returned to Neon until the dead tuples are cleaned up. Postgres
marks them dead; `VACUUM` makes the space reusable, and only `VACUUM FULL`
(exclusive lock, needs free space for a rewrite) returns it to the filesystem.
This script prints the follow-up rather than running it.

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/prune_old_embeddings.py                 # dry run, 90d
    ./.venv/bin/python3 scripts/prune_old_embeddings.py --older-than 30
    ./.venv/bin/python3 scripts/prune_old_embeddings.py --apply

Idempotent: only touches rows where the embedding is still present.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402

# The feed's own recency floor (sift/lib/db.ts, sift#172). A cutoff at or below
# this would strip embeddings from articles the feed still renders — harmless
# for the feed itself, which does not read the column, but it means the
# operator has almost certainly mistyped the argument.
FEED_FLOOR_DAYS = 30
CHUNK = 5000


async def main(days: int, apply: bool, chunk: int) -> int:
    if days < FEED_FLOOR_DAYS:
        print(f"REFUSING: --older-than {days} is inside the feed's own {FEED_FLOOR_DAYS}-day "
              f"floor. Articles that recent are still on screen; if you really mean it, "
              f"raise {FEED_FLOOR_DAYS} in this script and say why.", file=sys.stderr)
        return 1

    db_url = settings.database_url
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        before = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        row = await conn.fetchrow(f"""
            SELECT count(*) n,
                   pg_size_pretty(coalesce(sum(pg_column_size(embedding)), 0)) bytes
            FROM articles
            WHERE embedding IS NOT NULL
              AND created_at < NOW() - INTERVAL '{days} days'
        """)
        keep = await conn.fetchval(f"""
            SELECT count(*) FROM articles
            WHERE embedding IS NOT NULL
              AND created_at >= NOW() - INTERVAL '{days} days'
        """)
        searches = await conn.fetchval("SELECT count(*) FROM search_queries") or 0

        print(f"database now            {before}")
        print(f"cutoff                  {days} days")
        print(f"embeddings to clear     {row['n']:,}  ({row['bytes']} of column data)")
        print(f"embeddings kept         {keep:,}  — still vector-searchable")
        print(f"search_queries ever     {searches}  — the usage this trades against")

        if row["n"] == 0:
            print("\nNothing to do.")
            return 0

        if not apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            print("Reversible afterwards via scripts/backfill_embeddings.py "
                  "(rebuilds from title + summary, both kept).")
            return 0

        cleared = 0
        while True:
            # Chunked so each statement is short and an interrupted run simply
            # resumes — the WHERE clause already excludes rows that are done.
            # conn.execute returns "UPDATE <n>"; asyncpg has no rowcount.
            status = await conn.execute(f"""
                WITH batch AS (
                    SELECT id FROM articles
                    WHERE embedding IS NOT NULL
                      AND created_at < NOW() - INTERVAL '{days} days'
                    LIMIT {chunk}
                )
                UPDATE articles a SET embedding = NULL
                FROM batch WHERE a.id = batch.id
            """)
            n = int(status.rsplit(" ", 1)[-1])
            if n == 0:
                break
            cleared += n
            print(f"  cleared {cleared:,} / {row['n']:,}")

        after = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        print(f"\ncleared {cleared:,} embeddings. database {before} -> {after}")
        print("\nSpace is not returned yet — the tuples are dead, not gone. Next:")
        print("  VACUUM (ANALYZE) articles;      -- makes the space reusable, no lock")
        print("  VACUUM FULL articles;           -- returns it to Neon; EXCLUSIVE lock, needs")
        print("                                  -- free space to rewrite the table")
        print("  REINDEX INDEX CONCURRENTLY idx_articles_embedding;  -- the 879 MB one")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--older-than", type=int, default=90, dest="days",
                   help="clear embeddings on articles older than N days (default 90)")
    p.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    p.add_argument("--chunk", type=int, default=CHUNK, help=f"rows per statement (default {CHUNK})")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.days, args.apply, args.chunk)))
