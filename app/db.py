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


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
