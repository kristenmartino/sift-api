"""
One-shot retroactive pass for articles.is_roundup (migrations/024).

Run from sift-api root:
    python scripts/backfill_roundup.py            # report, no writes
    python scripts/backfill_roundup.py --apply    # write the flags

Deterministic, no LLM. Uses services/genre.py:detect_roundup directly (in
Python, not mirrored SQL — the pattern list is longer than the opinion
one and titles are small; a full scan in batches is cheap).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from app.config import settings  # noqa: E402
from services.genre import detect_roundup  # noqa: E402


async def main() -> None:
    apply = "--apply" in sys.argv
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        rows = await conn.fetch(
            "SELECT id, source_name, title FROM articles WHERE is_roundup = false"
        )
        hits = [(r["id"], r["source_name"]) for r in rows if detect_roundup(r["title"])]
        by_source: dict[str, int] = {}
        for _, src in hits:
            by_source[src] = by_source.get(src, 0) + 1
        print(f"{len(hits)} unflagged articles match the roundup patterns:")
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {src}: {n}")
        if apply and hits:
            ids = [h[0] for h in hits]
            result = await conn.execute(
                "UPDATE articles SET is_roundup = true WHERE id = ANY($1::text[])", ids
            )
            print(f"APPLIED: {result}")
        elif not apply:
            print("(dry run — pass --apply to write)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
