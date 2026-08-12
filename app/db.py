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

        # Cited org claims (migrations/012_org_self_description.sql).
        # Replaces the Sift-assigned org_profiles.political_lean, which was
        # hand-authored and rendered uncited — Sift computing its own political
        # rating, contrary to D37. self_description holds the organization's own
        # words verbatim; governance_structure holds statutory facts for
        # agencies. Both render only alongside their source URL (the frontend
        # parser in sift/lib/org.ts nulls the pair otherwise), so an uncited
        # characterization of a real organization cannot reach a page.
        # political_lean is retained for rollback and no longer rendered.
        await conn.execute("""
            ALTER TABLE org_profiles
              ADD COLUMN IF NOT EXISTS self_description         TEXT,
              ADD COLUMN IF NOT EXISTS self_description_source  TEXT,
              ADD COLUMN IF NOT EXISTS self_description_checked DATE,
              ADD COLUMN IF NOT EXISTS governance_structure     TEXT,
              ADD COLUMN IF NOT EXISTS governance_source        TEXT
        """)

        # Budget provenance + political_lean removal (migrations/013).
        # annual_budget_usd now means total functional expenses from a specific
        # Form 990 and cannot render without the fiscal year and source that
        # make it checkable. political_lean is DROPPED, not deprecated:
        # migration 012 left it in place and /civic went on publishing it for
        # all 103 orgs, because a column that still exists is one something can
        # still read.
        await conn.execute("""
            ALTER TABLE org_profiles
              ADD COLUMN IF NOT EXISTS annual_budget_fy     TEXT,
              ADD COLUMN IF NOT EXISTS annual_budget_source TEXT
        """)
        await conn.execute("ALTER TABLE org_profiles DROP COLUMN IF EXISTS political_lean")

        # Curated surface-form aliases for the entity linker
        # (migrations/014_entity_aliases.sql).
        # The 2026-08-05 audit found entity_links on only 7.6% of articles,
        # much of it names rather than missing dossiers: "Pentagon" (756
        # articles) never chips although united-states-department-of-defense
        # exists. entity_linker.py matches the full canonical name only —
        # correct as a default (see politician_aliases() on why *derived*
        # last-name aliases were removed in #40), so this is the curated
        # alternative that docstring anticipated. Populated from
        # data/entity_aliases.csv via scripts/seed_entity_aliases.py.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_aliases (
              alias         TEXT PRIMARY KEY,
              entity_type   TEXT NOT NULL
                            CHECK (entity_type IN ('politician', 'org', 'bill', 'outlet')),
              canonical_id  TEXT NOT NULL,
              notes         TEXT,
              added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_aliases_target "
            "ON entity_aliases (entity_type, canonical_id)"
        )

        # Per-alias case-sensitive matching
        # (migrations/017_entity_alias_match_case.sql).
        # `_word_pattern` compiles every key with IGNORECASE, which makes an
        # acronym whose lowercase form is an ordinary word unusable: the
        # us-immigration-and-customs-enforcement dossier has existed all along,
        # yet ICE links in 8 of 300 sampled articles, because the alias `ice`
        # would also fire on ice cream, sea ice and hockey across the corpus's
        # 85k sports and 48k entertainment rows. Measured 2026-08-07: 1,725
        # articles carry whole-word uppercase ICE, 15 of 15 sampled were the
        # agency, and 0 titles are all-caps. Also the mechanism sift-api#151
        # asked for on `variety` / `the athletic` / `wired` / `the hill`.
        # DEFAULT FALSE, so all 96 existing rows keep the behaviour they were
        # curated under.
        await conn.execute(
            "ALTER TABLE entity_aliases "
            "ADD COLUMN IF NOT EXISTS match_case BOOLEAN NOT NULL DEFAULT FALSE"
        )

        # Structured, primary-record role provenance on executive dossiers
        # (migrations/015_politician_role_provenance.sql).
        # All 102 chamber IN ('executive','foreign-executive') rows carried an
        # uncited `notes` blob of biographical claims about living people —
        # the org_profiles.notes defect (STATUS.md:103) that migration 013
        # removed, reproduced in a later population, and forbidden outright by
        # OPERATING_CONTEXT.md §5. Each claim-bearing column below is paired
        # with a source column on the 013 pattern, so a value cannot render
        # without the record that backs it. Populated from senate.gov
        # roll-calls + api.congress.gov by scripts/scrape_executive_records.py
        # and written by scripts/seed_executive_records.py, which clears
        # `notes` on those rows in the same transaction.
        await conn.execute("""
            ALTER TABLE politician_profiles
              ADD COLUMN IF NOT EXISTS id_source                TEXT,
              ADD COLUMN IF NOT EXISTS role_title               TEXT,
              ADD COLUMN IF NOT EXISTS role_title_source        TEXT,
              ADD COLUMN IF NOT EXISTS role_start_date          DATE,
              ADD COLUMN IF NOT EXISTS role_end_date            DATE,
              ADD COLUMN IF NOT EXISTS role_dates_source        TEXT,
              ADD COLUMN IF NOT EXISTS nomination_date          DATE,
              ADD COLUMN IF NOT EXISTS nomination_url           TEXT,
              ADD COLUMN IF NOT EXISTS confirmation_date        DATE,
              ADD COLUMN IF NOT EXISTS confirmation_vote_url    TEXT,
              ADD COLUMN IF NOT EXISTS confirmation_vote_result TEXT,
              ADD COLUMN IF NOT EXISTS predecessor_name         TEXT,
              ADD COLUMN IF NOT EXISTS predecessor_source       TEXT
        """)
        # Matches the publish gate in sift/lib/db.ts listSitemapEntries, which
        # filters chamber first and then requires role_title + its source.
        # When a role source was last refetched, and the expiry the publish
        # floor reads (migrations/017_role_verified_at.sql). Only
        # foreign-executive rows expire: their source is a live page that names
        # the person, so it stops being true when they leave office. US and
        # scotus rows rest on a statute plus a permanent Senate roll-call, and
        # their departures are caught by the successor's confirmation instead.
        await conn.execute(
            "ALTER TABLE politician_profiles "
            "ADD COLUMN IF NOT EXISTS role_verified_at DATE"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_politician_profiles_role_verified "
            "ON politician_profiles (chamber, role_verified_at) "
            "WHERE role_title IS NOT NULL AND role_title_source IS NOT NULL"
        )

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_politician_profiles_sourced_role "
            "ON politician_profiles (chamber) "
            "WHERE role_title IS NOT NULL AND role_title_source IS NOT NULL"
        )

        # SCOTUS Justices move into politician_profiles; judge_profiles retires
        # (migrations/016_scotus_into_politician_profiles.sql).
        # judge_profiles was created by an unmerged branch (9f44ba2, Phase 3.J)
        # whose seeders were run against prod by hand, so the table exists in
        # production and in no repo — never in init.sql, never here, and main's
        # linker has no `judge` type, so its 87 entity_links could never be
        # refreshed and never rendered (entityLinks.ts drops unknown types).
        # Migration 015 already reserved id_source = 'scotus' and already gave
        # the reason a fifth entity type is not worth its cost. `notes` is NOT
        # carried over: all 9 rows held uncited characterizations of living
        # people, which OPERATING_CONTEXT.md §5 forbids. Sourced replacements
        # come from scripts/seed_scotus_records.py.
        # Conditional so a fresh init.sql DB — where judge_profiles has never
        # existed — is a clean no-op rather than an error.
        await conn.execute("""
            DO $$
            BEGIN
              IF to_regclass('public.judge_profiles') IS NOT NULL THEN
                INSERT INTO politician_profiles (bioguide_id, name, chamber, id_source,
                                                 external_links, refreshed_at, updated_at)
                SELECT canonical_id, name, 'scotus', 'scotus',
                       COALESCE(external_links, '{}'::jsonb) - 'wikipedia',
                       refreshed_at, updated_at
                  FROM judge_profiles
                ON CONFLICT (bioguide_id) DO NOTHING;

                DROP TABLE judge_profiles;
              END IF;
            END $$
        """)

        # Story-threading queue marker (migrations/017_articles_threaded_at.sql).
        # NULL = not yet considered. Lets threading consume a queue instead of
        # rescanning the 48h window every run, which is what made cost scale
        # with cadence rather than with new articles.
        #
        # A per-row marker rather than a timestamp watermark: entities land
        # asynchronously (1.58% pending at any moment), and a watermark would
        # advance past an article whose entities had not arrived, dropping it
        # permanently. A NULL simply waits.
        #
        # No backfill and no new index — the queue query bounds on
        # published_date > NOW() - 48h, so historical rows are never selected,
        # and idx_articles_category_date already serves that filter.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS threaded_at TIMESTAMPTZ"
        )

        # One row per incremental-threading shadow run
        # (migrations/018_threading_shadow.sql).
        #
        # The shadow report gates the cutover, and it used to live only in the
        # Railway log buffer — which rotates and resets on deploy, so a 24h
        # aggregate could not be reconstructed. Same failure ai_usage_daily
        # had: the number nobody could query was the number nobody checked.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS threading_shadow (
                run_at                            TIMESTAMPTZ PRIMARY KEY DEFAULT NOW(),
                backlog                           INTEGER NOT NULL,
                sampled                           INTEGER NOT NULL,
                attach_candidates                 INTEGER NOT NULL,
                new_cluster_candidates            INTEGER NOT NULL,
                new_clusters_passing_outlet_gate  INTEGER NOT NULL,
                parked                            INTEGER NOT NULL,
                parked_with_near_miss             INTEGER NOT NULL,
                llm_relevant                      INTEGER NOT NULL,
                threshold                         REAL,
                near_miss_floor                   REAL,
                llm_relevant_by_category          JSONB NOT NULL DEFAULT '{}'::jsonb,
                dry_run                           JSONB,
                would_group                       INTEGER,
                confirm_rate                      REAL
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_threading_shadow_run_at "
            "ON threading_shadow (run_at DESC)"
        )

        # How each Claude call ended, per day (migrations/021).
        #
        # Alignment re-asks on summarizer.batch run at 4-12% of calls and each
        # is a full-price duplicate; nothing recorded *why*. `max_tokens = 700`
        # against five ~60-token summaries is comfortable on a typical batch
        # and tight on five long ones, and a response cut off at the cap is
        # truncated JSON — which fails alignment exactly the way these retries
        # look. Same queryability argument as threading_shadow above.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_output_stops (
                usage_date        DATE    NOT NULL,
                operation         TEXT    NOT NULL,
                stop_reason       TEXT    NOT NULL,
                aligned           BOOLEAN NOT NULL,
                batch_size        INTEGER NOT NULL,
                call_count        INTEGER NOT NULL DEFAULT 0,
                max_output_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (usage_date, operation, stop_reason, aligned, batch_size)
            )
        """)

        # Interest-group rating provenance (migrations/019).
        # The column shipped as a bare {rater: score} dict — a claim about a
        # living person's voting record with no year and no citation, the same
        # defect 013 and 015 each had to remove. Never populated, so this is a
        # shape fix ahead of data, not a data migration. New shape is an array
        # of {rater, rater_name, score, unit, year, lifetime_score?,
        # source_url}; lib/politician.ts drops any entry missing score, year
        # or source_url.
        await conn.execute(
            "ALTER TABLE politician_profiles "
            "ALTER COLUMN interest_group_ratings SET DEFAULT '[]'::jsonb"
        )
        await conn.execute(
            "UPDATE politician_profiles "
            "SET interest_group_ratings = '[]'::jsonb "
            "WHERE interest_group_ratings IS NULL "
            "OR jsonb_typeof(interest_group_ratings) <> 'array'"
        )

        # Article tone signal (migrations/020). grim | neutral | light,
        # written by the same Haiku call as why_it_matters/importance_score;
        # NULL = unclassified, treated as neutral by every ranking site.
        # Feeds the D48 grim dampener in sift/lib/db.ts.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS tone TEXT "
            "CHECK (tone IN ('grim', 'neutral', 'light'))"
        )

        # Daily compare example (migrations/021). One row, refreshed at most
        # once per UTC day after a pipeline run — the anonymous door into the
        # compare feature. Read path is sift/lib/db.ts (landing demo + the
        # signed-out /news compare branch).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_compare_example (
                id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                payload JSONB NOT NULL,
                generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Feed-balance drift snapshots (migrations/022, ranking v2 stage 3).
        # Written daily by services/feed_balance.py; persisted so the
        # trailing-13-day tripwire baselines survive log rotation/deploys.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_balance (
                run_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                category              TEXT NOT NULL,
                grim_share_top10      REAL,
                mean_civic_top10      REAL,
                mean_sources_top5     REAL,
                story_grim_share_top5 REAL,
                n_articles            INTEGER NOT NULL DEFAULT 0,
                n_stories             INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_at, category)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_balance_run_at "
            "ON feed_balance (run_at DESC)"
        )

        # Opinion-genre flag (migrations/023, ranking v2 stage 4). Set at
        # store time by services/genre.py from outlet-declared markers; the
        # read path dampens opinion x0.6 and excludes it from the
        # cross-spectrum bonus. DEFAULT FALSE, not nullable — under a
        # precision-first heuristic, no marker IS the reported verdict.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS is_opinion "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await conn.execute(
            "ALTER TABLE feed_balance ADD COLUMN IF NOT EXISTS "
            "opinion_share_top10 REAL"
        )

        # Roundup/brief-container flag (migrations/024, ranking v2 stage 5).
        # Program episodes and daily briefs inherit importance from the
        # events they mention; the read path ranks them x0.4.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS is_roundup "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        )

        # Article genre (migrations/025, ranking v2 stage 6). news |
        # feature | soft, from the same Haiku call as tone. NULL = "news"
        # at every ranking site. Applied to standalone articles only.
        await conn.execute(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS genre TEXT "
            "CHECK (genre IN ('news', 'feature', 'soft'))"
        )

        # Failed-story retry counter (migrations/026, #211). Bounds the
        # sweeper in run_incremental_threading so a story that fails for a
        # structural reason is not re-paid for on every 30-minute run forever.
        await conn.execute(
            "ALTER TABLE stories ADD COLUMN IF NOT EXISTS "
            "synthesis_attempts INTEGER DEFAULT 0"
        )
        # Partial index over exactly the sweeper's predicate — a per-run query
        # selecting a handful of rows out of the whole table. Non-CONCURRENTLY
        # here (this runs inside startup); the .sql file carries the
        # CONCURRENTLY form for manual application against a live DB.
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stories_failed_sweep
                ON stories (synthesis_attempts, created_at DESC)
                WHERE synthesis_status = 'failed'
        """)

        # Funding edges between orgs, from 990 Schedule I / R (migrations/027).
        # Populated only by scripts/ingest_funding_edges.py — deliberately not
        # on the pipeline heartbeat, so an empty table is the normal state
        # until someone runs the ingest. ein_name_agrees carries the verdict
        # from services/funding_edges.py: an EIN is a join key a human typed,
        # and one of the first 121 edges pulled had the wrong one.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS funding_edges (
                id                   BIGSERIAL PRIMARY KEY,
                source_ein           TEXT NOT NULL,
                source_name          TEXT NOT NULL,
                target_ein           TEXT,
                target_name_as_filed TEXT,
                target_name_irs      TEXT,
                edge_kind            TEXT NOT NULL
                                     CHECK (edge_kind IN ('grant', 'related_org')),
                amount_usd           BIGINT,
                purpose              TEXT,
                exempt_code          TEXT,
                fiscal_period        TEXT NOT NULL,
                form                 TEXT NOT NULL,
                reported_by          TEXT NOT NULL DEFAULT 'source',
                match_method         TEXT NOT NULL DEFAULT 'ein',
                ein_name_agrees      TEXT NOT NULL
                                     CHECK (ein_name_agrees IN ('agrees', 'review', 'ein_absent')),
                object_id            TEXT NOT NULL,
                filing_url           TEXT NOT NULL,
                retrieved_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_edges_identity
                ON funding_edges (source_ein, target_ein, fiscal_period, form,
                                  COALESCE(amount_usd, -1))
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_funding_edges_publishable
                ON funding_edges (ein_name_agrees, target_ein)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_funding_edges_source
                ON funding_edges (source_ein, fiscal_period DESC)
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
