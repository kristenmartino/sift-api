"""One-shot script to backfill articles.context_primer for existing rows.

Run from sift-api root:
    ./.venv/bin/python3 scripts/backfill_primers.py [--limit N]

Optionally pass DATABASE_URL as an env var to target a different database.

Defaults to the most recent 200 articles missing a primer (the figure cited
in plans/sift-civic-literacy.md). Override with --limit to backfill more or
fewer; pass --limit 0 to process every article missing a primer.

Cost (rough): ~ $0.005 per article in Haiku tier × 200 = ~$1 for the default
backfill. Live API path (not the batch API) so it completes synchronously.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings
from services.primer_generator import generate_primers


async def main(limit: int) -> None:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    where_limit = f"LIMIT {limit}" if limit > 0 else ""

    rows = await pool.fetch(
        f"""
        SELECT source_url, source_name, title, summary
        FROM articles
        WHERE context_primer IS NULL
          AND summary IS NOT NULL
          AND summary != ''
          AND LOWER(summary) NOT LIKE 'unable to provide%'
        ORDER BY published_date DESC NULLS LAST, created_at DESC
        {where_limit}
        """
    )

    if not rows:
        print("No articles need primer backfilling.")
        await pool.close()
        return

    print(f"Found {len(rows)} articles to backfill primers for...")

    articles = [
        {
            "source_url": r["source_url"],
            "source_name": r["source_name"],
            "title": r["title"],
            "summary": r["summary"],
        }
        for r in rows
    ]

    # Process in chunks so progress is visible and partial failures don't lose
    # earlier work. CHUNK_SIZE * BATCH_SIZE in primer_generator = articles per
    # API call burst.
    CHUNK_SIZE = 50
    total_updated = 0
    total_skipped = 0  # primer came back empty (genuinely no context needed)
    total_chunks = (len(articles) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for chunk_start in range(0, len(articles), CHUNK_SIZE):
        chunk = articles[chunk_start : chunk_start + CHUNK_SIZE]
        chunk_num = chunk_start // CHUNK_SIZE + 1
        print(f"  Chunk {chunk_num}/{total_chunks} ({len(chunk)} articles)...")

        results = await generate_primers(chunk)
        print(f"    Generated {len(results)} primers, writing to DB...")

        for source_url, primer in results.items():
            try:
                await pool.execute(
                    "UPDATE articles SET context_primer = $1::jsonb, updated_at = NOW() "
                    "WHERE source_url = $2",
                    json.dumps(primer),
                    source_url,
                )
                total_updated += 1
            except Exception as e:
                print(f"    UPDATE failed for {source_url}: {e}")

        # Articles in the chunk for which the LLM returned no primer (because
        # the article needed no civic context) — track but don't write.
        chunk_urls = {a["source_url"] for a in chunk}
        total_skipped += len(chunk_urls - results.keys())

        print(f"    Progress: {total_updated} updated, {total_skipped} skipped (empty).")

    print(
        f"\nDone! Updated {total_updated} articles with context_primer; "
        f"{total_skipped} returned empty (no primer needed)."
    )

    await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill context_primer for existing articles.")
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max articles to process. 0 = no limit. Default: 200 (per civic-literacy plan).",
    )
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit))
