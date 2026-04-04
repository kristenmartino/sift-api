"""
One-shot script to remove existing articles whose summaries indicate
insufficient content (e.g. "insufficient content to evaluate",
"unable to provide a summary", etc.).

Run from sift-api root:  python scripts/cleanup_insufficient_content.py

Optionally pass DATABASE_URL as env var to target a different database.
Set DRY_RUN=1 to preview what would be deleted without making changes.
"""
import asyncio
import os
import sys

# Add parent dir to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from app.config import settings
from services.summarizer import _INSUFFICIENT_CONTENT_PHRASES


async def main():
    dry_run = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    # Build WHERE clause matching any of the insufficient-content phrases
    conditions = " OR ".join(
        f"LOWER(summary) LIKE '%{phrase}%'" for phrase in _INSUFFICIENT_CONTENT_PHRASES
    )
    # Also catch the exact marker the updated prompt now emits
    conditions += " OR LOWER(summary) = 'insufficient_content'"
    # Also catch empty/null summaries
    conditions = f"({conditions}) OR summary IS NULL OR TRIM(summary) = ''"

    query = f"SELECT id, title, summary, source_url FROM articles WHERE {conditions}"
    rows = await pool.fetch(query)

    if not rows:
        print("No articles with insufficient content found. Database is clean.")
        await pool.close()
        return

    print(f"Found {len(rows)} article(s) with insufficient content:\n")
    for row in rows:
        summary_preview = (row["summary"] or "(null)")[:80]
        print(f"  - {row['title'][:60]}")
        print(f"    Summary: {summary_preview}")
        print(f"    URL: {row['source_url']}\n")

    if dry_run:
        print("DRY_RUN is set — no changes made.")
        await pool.close()
        return

    # First detach from any stories (clear story_id reference)
    article_ids = [row["id"] for row in rows]
    await pool.execute(
        "UPDATE articles SET story_id = NULL WHERE id = ANY($1::text[])",
        article_ids,
    )

    # Delete the articles
    deleted = await pool.execute(
        "DELETE FROM articles WHERE id = ANY($1::text[])",
        article_ids,
    )

    print(f"Deleted {len(article_ids)} articles. ({deleted})")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
