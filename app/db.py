from __future__ import annotations

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
    )
    await _apply_migrations(_pool)


async def _apply_migrations(pool: asyncpg.Pool) -> None:
    """Idempotent schema migrations run at startup.

    Keeping these here (rather than a separate migration runner) lets Railway's
    existing DB pick up additive columns on the next deploy without manual ops.
    """
    async with pool.acquire() as conn:
        # Phase 4: content-hash dedup column + lookup index.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS content_hash TEXT"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_content_hash "
            "ON articles(content_hash)"
        )

        # Phase 6: Message Batches tracking table (50% cost discount).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_batches (
                batch_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                submitted_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                metadata JSONB DEFAULT '{}'::jsonb
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_batches_status_kind "
            "ON api_batches(status, kind)"
        )

        # Feed indexes (migrations/004_feed_indexes.sql). Partial indexes that
        # match the exact predicates in sift/lib/db.ts's user-facing queries,
        # so category feeds don't fall back to sequential scans on articles.
        # CREATE INDEX (without CONCURRENTLY) is fine here: asyncpg runs each
        # execute() in autocommit, and IF NOT EXISTS makes repeat deploys a
        # no-op. CONCURRENTLY lives in the SQL file for operators who prefer
        # to apply the migration manually against a live DB.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_feed "
            "ON articles (category, published_date DESC) "
            "WHERE from_search = false "
            "AND summary IS NOT NULL "
            "AND summary <> ''"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_story_feed "
            "ON articles (story_id) "
            "WHERE story_id IS NOT NULL "
            "AND from_search = false "
            "AND summary IS NOT NULL "
            "AND summary <> ''"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stories_feed "
            "ON stories (category, published_date DESC) "
            "WHERE synthesis_status = 'complete'"
        )


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
