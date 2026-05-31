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

        # Civic-literacy MVP (migrations/005_context_primer_and_reading_levels.sql).
        # context_primer holds the "What you should know first" panel data;
        # reading_levels holds Claude rewrites at simpler + detailed reading
        # levels for long-form articles. Both nullable — UI tolerates NULL.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS context_primer JSONB"
        )
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS reading_levels JSONB"
        )

        # Outlet provenance (migrations/006_outlet_profiles.sql).
        # outlet_profiles: curated metadata for the ~50 outlets Sift ingests
        # from. source_name_aliases: maps messy RSS source_name values onto
        # canonical outlet_slug. Both populated from the data/outlet_profiles.csv
        # template via scripts/seed_outlet_profiles.py.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS outlet_profiles (
                slug                  TEXT PRIMARY KEY,
                name                  TEXT NOT NULL,
                parent_company        TEXT,
                parent_company_url    TEXT,
                founded_year          INT,
                funding_model         TEXT,
                major_funders         JSONB DEFAULT '[]'::jsonb,
                allsides_rating       TEXT,
                allsides_url          TEXT,
                allsides_last_checked DATE,
                mbfc_factual          TEXT,
                mbfc_url              TEXT,
                mbfc_last_checked     DATE,
                notes                 TEXT,
                external_links        JSONB DEFAULT '{}'::jsonb,
                updated_at            TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outlet_profiles_name_lower "
            "ON outlet_profiles (LOWER(name))"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS source_name_aliases (
                raw_source_name TEXT PRIMARY KEY,
                outlet_slug     TEXT NOT NULL REFERENCES outlet_profiles (slug) ON DELETE CASCADE,
                added_at        TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_name_aliases_slug "
            "ON source_name_aliases (outlet_slug)"
        )

        # Phase 3.A — politician + org + bill curated profile tables
        # (migrations/007_politician_org_bill_profiles.sql).
        # Schema only here; population is staged across Phase 3.B (GovTrack
        # scrape for politicians), 3.D (manual org curation), 3.E
        # (OpenSecrets enrichment), 3.F (on-demand bill fetch).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS politician_profiles (
                bioguide_id                  TEXT PRIMARY KEY,
                name                         TEXT NOT NULL,
                party                        TEXT,
                state                        TEXT,
                chamber                      TEXT,
                committees                   JSONB DEFAULT '[]'::jsonb,
                top_industries_current_cycle JSONB DEFAULT '[]'::jsonb,
                interest_group_ratings       JSONB DEFAULT '{}'::jsonb,
                external_links               JSONB DEFAULT '{}'::jsonb,
                notes                        TEXT,
                refreshed_at                 TIMESTAMPTZ DEFAULT NOW(),
                updated_at                   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_politician_profiles_name_lower "
            "ON politician_profiles (LOWER(name))"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_politician_profiles_state_party "
            "ON politician_profiles (state, party)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_politician_profiles_chamber "
            "ON politician_profiles (chamber)"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS org_profiles (
                slug              TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                type              TEXT,
                political_lean    TEXT,
                founded_year      INT,
                annual_budget_usd NUMERIC,
                major_funders     JSONB DEFAULT '[]'::jsonb,
                fara_registered   BOOLEAN DEFAULT FALSE,
                fara_countries    JSONB DEFAULT '[]'::jsonb,
                external_links    JSONB DEFAULT '{}'::jsonb,
                notes             TEXT,
                updated_at        TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_profiles_name_lower "
            "ON org_profiles (LOWER(name))"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_profiles_type "
            "ON org_profiles (type)"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bill_profiles (
                bill_id               TEXT PRIMARY KEY,
                congress              INT NOT NULL,
                title                 TEXT NOT NULL,
                short_title           TEXT,
                sponsor_bioguide      TEXT REFERENCES politician_profiles (bioguide_id) ON DELETE SET NULL,
                cosponsors            JSONB DEFAULT '[]'::jsonb,
                status                TEXT,
                introduced_date       DATE,
                lobbying_for_usd      NUMERIC,
                lobbying_against_usd  NUMERIC,
                external_links        JSONB DEFAULT '{}'::jsonb,
                notes                 TEXT,
                refreshed_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at            TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bill_profiles_sponsor "
            "ON bill_profiles (sponsor_bioguide)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bill_profiles_congress "
            "ON bill_profiles (congress)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bill_profiles_short_title_lower "
            "ON bill_profiles (LOWER(short_title))"
        )

        # Phase 3.G — articles.entity_links
        # (migrations/008_article_entity_links.sql).
        # Denormalized JSONB column populated by the entity_linker pipeline
        # node. Frontend (sift Phase 3.H InlineGlossaryTooltip) reads this
        # to render hover/tap context panels for politicians/orgs/bills/
        # outlets mentioned in each article. GIN index supports the inverse
        # query ("which articles mention this entity") for dossier pages.
        await conn.execute(
            "ALTER TABLE articles "
            "ADD COLUMN IF NOT EXISTS entity_links JSONB DEFAULT '[]'::jsonb"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_entity_links_gin "
            "ON articles USING gin(entity_links)"
        )

        # Search-funnel instrumentation (migrations/009_search_queries.sql).
        # Phase 1 of search-improvement plan: log every topic-search query
        # so we can see what users actually look for and decide whether
        # the next investment should be entity-aware resolution (Phase 2)
        # or HNSW + re-ranking (Phase 3). Raw IPs are never persisted —
        # the sift route hashes them with HMAC-SHA256 before INSERT.
        # 90-day retention enforced by scripts/cleanup_old_search_queries.py.
        await conn.execute(
            "CREATE EXTENSION IF NOT EXISTS pgcrypto"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_queries (
              id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
              created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              query                   TEXT NOT NULL,
              query_norm              TEXT NOT NULL,
              query_token_count       INT NOT NULL,
              result_count_vector     INT NOT NULL,
              result_count_total      INT NOT NULL,
              fallback_used           BOOLEAN NOT NULL DEFAULT FALSE,
              latency_ms_total        INT NOT NULL,
              latency_ms_embed        INT,
              latency_ms_vector       INT,
              latency_ms_fallback     INT,
              session_id              TEXT,
              ip_hash                 TEXT,
              user_agent_class        TEXT,
              matched_entity_type     TEXT,
              matched_entity_id       TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_queries_created "
            "ON search_queries(created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_search_queries_query_norm "
            "ON search_queries(query_norm)"
        )

        # Primer-expand instrumentation (migrations/010_primer_expand_events.sql).
        # We've shipped three rounds of primer-content work without ever
        # knowing whether anyone opens the panel. This table records every
        # panel-expand click; impressions are NOT tracked (computable from
        # articles.context_primer IS NOT NULL at query time, no need to
        # write ~5k rows/day). Privacy posture mirrors search_queries:
        # IPs hashed, 90-day retention via scripts/cleanup_old_primer_events.py.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS primer_expand_events (
              id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
              created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              article_id              TEXT,
              surface                 TEXT,
              session_id              TEXT,
              ip_hash                 TEXT,
              user_agent_class        TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_primer_expand_events_created "
            "ON primer_expand_events(created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_primer_expand_events_article "
            "ON primer_expand_events(article_id) WHERE article_id IS NOT NULL"
        )

        # Daily AI cost ledger (migrations/011_ai_usage_daily.sql).
        # One row per (UTC date, provider, model, operation). cost_guard sums
        # the day's estimated_cost_usd to enforce the daily ceiling (sift-api#70)
        # and alert at 80%. Covers the live paid paths (compare web-search +
        # Voyage embeddings); frontend topic-search stays a D35 exception
        # (sift-api#79) until that fallback moves into sift-api.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage_daily (
                usage_date          DATE NOT NULL,
                provider            TEXT NOT NULL,
                model               TEXT NOT NULL,
                operation           TEXT NOT NULL,
                estimated_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
                call_count          INTEGER NOT NULL DEFAULT 0,
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (usage_date, provider, model, operation)
            )
        """)


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
