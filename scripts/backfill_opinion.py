"""
One-shot retroactive pass for articles.is_opinion (migrations/023).

Run from sift-api root:
    python scripts/backfill_opinion.py            # report matches, no writes
    python scripts/backfill_opinion.py --apply    # write the flags

Deterministic and free — no LLM. The SQL patterns below mirror
services/genre.py's regexes (keep them in lockstep): outlet-declared
opinion markers only, in the URL path or as a title prefix. New articles
get the flag at store time; this covers everything already in the table,
since source_url and title are stored.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from app.config import settings  # noqa: E402

# Lockstep with services/genre.py: _OPINION_PATH and _OPINION_TITLE.
WHERE = r"""
    is_opinion = false
    AND (source_url ~* '://[^/?#]+/([^?#]*/)?(opinion|opinions|commentary|editorial|editorials|op-ed|commentisfree)(/|$|[?#])'
         OR title ~* '^\s*(opinion|editorial|comment)\s*[:|]')
"""


async def main() -> None:
    apply = "--apply" in sys.argv
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        rows = await conn.fetch(
            f"SELECT source_name, COUNT(*) AS n FROM articles WHERE {WHERE} "
            "GROUP BY 1 ORDER BY n DESC"
        )
        total = sum(r["n"] for r in rows)
        print(f"{total} unflagged articles match the opinion patterns:")
        for r in rows[:15]:
            print(f"  {r['source_name']}: {r['n']}")
        if apply and total:
            result = await conn.execute(f"UPDATE articles SET is_opinion = true WHERE {WHERE}")
            print(f"APPLIED: {result}")
        elif not apply:
            print("(dry run — pass --apply to write)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
