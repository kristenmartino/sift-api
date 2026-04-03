"""
One-shot script to backfill why_it_matters + importance_score for existing articles.
Run from sift-api root: python scripts/backfill_context.py
Optionally pass DATABASE_URL as env var to target a different database.
"""
import asyncio
import os
import sys

# Add parent dir to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from services.context_generator import generate_context
from app.config import settings


async def main():
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    # Neon requires explicit ssl kwarg for asyncpg
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    # Get all articles missing importance_score (covers both fields)
    rows = await pool.fetch(
        """
        SELECT source_url, title, summary
        FROM articles
        WHERE importance_score IS NULL
          AND summary IS NOT NULL
          AND summary != ''
        ORDER BY published_date DESC NULLS LAST
        """
    )

    if not rows:
        print("No articles need backfilling.")
        await pool.close()
        return

    print(f"Found {len(rows)} articles to backfill...")

    articles = [
        {
            "source_url": r["source_url"],
            "title": r["title"],
            "summary": r["summary"],
        }
        for r in rows
        if r["summary"] and not r["summary"].lower().startswith("unable to provide")
    ]

    if not articles:
        print("No valid articles to process after filtering.")
        await pool.close()
        return

    print(f"Processing {len(articles)} articles (filtered out bad summaries)...")

    # Process in chunks of 100 (10 batches of 10) to show progress and commit incrementally
    CHUNK_SIZE = 100
    total_updated = 0
    for chunk_start in range(0, len(articles), CHUNK_SIZE):
        chunk = articles[chunk_start : chunk_start + CHUNK_SIZE]
        chunk_num = chunk_start // CHUNK_SIZE + 1
        total_chunks = (len(articles) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"  Chunk {chunk_num}/{total_chunks} ({len(chunk)} articles)...")

        results = await generate_context(chunk)
        print(f"    Generated {len(results)} results, writing to DB...")

        for source_url, data in results.items():
            await pool.execute(
                "UPDATE articles SET why_it_matters = $1, importance_score = $2 WHERE source_url = $3",
                data["context"],
                data["score"],
                source_url,
            )
            total_updated += 1

        print(f"    Progress: {total_updated} articles updated so far.")

    print(f"\nDone! Updated {total_updated} articles with context + importance score.")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
