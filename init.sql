-- Sift v2 database schema
-- Postgres 16 + pgvector

CREATE EXTENSION IF NOT EXISTS vector;

-- Articles: the core content table
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    source_url TEXT UNIQUE,
    source_name TEXT,
    image_url TEXT,
    category TEXT NOT NULL,
    published_date TIMESTAMPTZ,
    embedding VECTOR(512),
    read_time INTEGER DEFAULT 1,
    from_search BOOLEAN NOT NULL DEFAULT false,
    story_id TEXT,
    entities JSONB DEFAULT '[]'::jsonb,
    entity_links JSONB DEFAULT '[]'::jsonb,  -- resolved dossier refs (migrations/008)
    why_it_matters TEXT,
    context_primer JSONB,                    -- "What you should know first" panel (migrations/005)
    reading_levels JSONB,                    -- simpler/detailed rewrites, long-form only (migrations/005)
    importance_score INTEGER,
    tone TEXT CHECK (tone IN ('grim', 'neutral', 'light')),  -- D48 dampener input (migrations/020); NULL = unclassified = neutral
    is_opinion BOOLEAN NOT NULL DEFAULT FALSE,  -- outlet-declared opinion marker (migrations/023); read path dampens and excludes from spectrum bonus
    is_roundup BOOLEAN NOT NULL DEFAULT FALSE,  -- program-episode/brief container (migrations/024); read path ranks x0.4
    genre TEXT CHECK (genre IN ('news', 'feature', 'soft')),  -- writing kind (migrations/025); NULL = news; standalone articles only
    content_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- entity_links (migrations/008) is denormalized onto the article rather than
-- kept in a join table: every feed fetch in sift/lib/db.ts already returns the
-- column inline, so the read path needs no extra JOIN.
--   [{"type": "politician", "canonical_id": "S000148", "surface_form": "Chuck Schumer"}, ...]
-- type is one of politician|org|bill|outlet, matching the four dossier tables
-- below. Written by services/entity_linker.py at ingest.
--
-- context_primer / reading_levels (migrations/005) are nullable and the UI
-- tolerates NULL — short wires never get reading_levels at all.
--   context_primer: { background, terms: [{term, definition, source?}], generated_at }
--   reading_levels: { simpler: {headline, summary}, detailed: {...}, generated_at }

CREATE INDEX IF NOT EXISTS idx_articles_category_date
    ON articles(category, published_date DESC);

CREATE INDEX IF NOT EXISTS idx_articles_content_hash
    ON articles(content_hash);

-- Note: IVFFlat index requires rows to exist for training.
-- Run after initial data load:
-- CREATE INDEX idx_articles_embedding ON articles
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);

CREATE INDEX IF NOT EXISTS idx_articles_story_id
    ON articles(story_id);

-- Feed indexes (migrations/004). Partial indexes matching the exact predicates
-- in sift/lib/db.ts's user-facing queries. Without them the planner reverts to
-- sequential scans on articles and the slower categories blow past the 10s
-- client abort (API_TIMEOUT_MS in sift/lib/constants.ts). The migration file
-- uses CONCURRENTLY for live DBs; a fresh DB has no writes to block.
-- scripts/explain_feed_queries.py runs in CI on PRs that touch these.

-- Feed-quality articles by category + recency: getArticlesByCategory
-- (lib/db.ts:36) and the standalone-articles query (lib/db.ts:150).
CREATE INDEX IF NOT EXISTS idx_articles_feed
    ON articles (category, published_date DESC)
    WHERE from_search = false
      AND summary IS NOT NULL
      AND summary <> '';

-- Feed-quality articles by story_id: the LEFT JOIN in the stories query
-- (lib/db.ts:85) and the `story_id = ANY($1)` fetch (lib/db.ts:121).
CREATE INDEX IF NOT EXISTS idx_articles_story_feed
    ON articles (story_id)
    WHERE story_id IS NOT NULL
      AND from_search = false
      AND summary IS NOT NULL
      AND summary <> '';

-- Inverse of entity_links: "which articles mention this entity", for the
-- dossier pages' recent-articles sections (migrations/008).
CREATE INDEX IF NOT EXISTS idx_articles_entity_links_gin
    ON articles USING gin(entity_links);

-- Stories: grouped multi-source coverage of the same event
CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,
    framings JSONB DEFAULT '[]'::jsonb,
    entities JSONB DEFAULT '[]'::jsonb,
    article_count INTEGER DEFAULT 0,
    representative_image_url TEXT,
    published_date TIMESTAMPTZ,
    synthesis_status TEXT DEFAULT 'pending',
    -- Retries spent by the failed-story sweeper (migrations/026, #211).
    synthesis_attempts INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- The sweeper's population: 'failed' rows still under the retry bound.
CREATE INDEX IF NOT EXISTS idx_stories_failed_sweep
    ON stories (synthesis_attempts, created_at DESC)
    WHERE synthesis_status = 'failed';

CREATE INDEX IF NOT EXISTS idx_stories_category_date
    ON stories(category, published_date DESC);

-- Complete stories by category + recency: the outer filter in
-- getStoriesWithArticles (lib/db.ts:85), narrowed to rows the UI renders
-- (migrations/004).
CREATE INDEX IF NOT EXISTS idx_stories_feed
    ON stories (category, published_date DESC)
    WHERE synthesis_status = 'complete';

-- Add FK after stories table exists
ALTER TABLE articles ADD CONSTRAINT fk_articles_story
    FOREIGN KEY (story_id) REFERENCES stories(id);

-- Custom topics: user-defined search topics with embeddings
CREATE TABLE IF NOT EXISTS custom_topics (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    embedding VECTOR(512),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, name)
);

-- Row-Level Security: users can only access their own custom topics
ALTER TABLE custom_topics ENABLE ROW LEVEL SECURITY;

CREATE POLICY custom_topics_user_isolation ON custom_topics
    USING (user_id = current_setting('app.current_user_id', true))
    WITH CHECK (user_id = current_setting('app.current_user_id', true));

-- Bookmarks: user-saved articles
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, article_id)
);

-- Row-Level Security: users can only access their own bookmarks
ALTER TABLE bookmarks ENABLE ROW LEVEL SECURITY;

CREATE POLICY bookmarks_user_isolation ON bookmarks
    USING (user_id = current_setting('app.current_user_id', true))
    WITH CHECK (user_id = current_setting('app.current_user_id', true));

CREATE INDEX IF NOT EXISTS idx_bookmarks_user
    ON bookmarks(user_id, created_at DESC);

-- Pipeline state: tracks last refresh per category
CREATE TABLE IF NOT EXISTS pipeline_state (
    category TEXT PRIMARY KEY,
    last_refreshed_at TIMESTAMPTZ,
    article_count INTEGER DEFAULT 0,
    error TEXT
);

-- Seed pipeline_state with all 10 categories
INSERT INTO pipeline_state (category) VALUES
    ('top'), ('technology'), ('business'), ('science'),
    ('energy'), ('world'), ('health'), ('politics'),
    ('sports'), ('entertainment')
ON CONFLICT (category) DO NOTHING;

-- In-flight Anthropic Message Batches (50% cost discount, up to 24h SLA).
-- Rows stay until the poller marks them 'succeeded' or 'errored'.
CREATE TABLE IF NOT EXISTS api_batches (
    batch_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                        -- e.g. 'context', 'entity'
    status TEXT NOT NULL DEFAULT 'processing', -- processing|succeeded|errored|expired|canceled
    submitted_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb         -- optional per-batch notes
);

CREATE INDEX IF NOT EXISTS idx_api_batches_status_kind
    ON api_batches(status, kind);

-- Search-funnel instrumentation (migrations/009_search_queries.sql).
-- Phase 1 of search-improvement plan: one row per topic-search request
-- so we can see what users actually look for. Raw IPs are NEVER stored —
-- the sift route hashes them with HMAC-SHA256 before INSERT. Query text
-- is verbatim (needed for top-query rollups + eval-set generation);
-- 90-day retention enforced by scripts/cleanup_old_search_queries.py.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS search_queries (
    id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    query                   TEXT NOT NULL,             -- raw, max 200 chars per route guard
    query_norm              TEXT NOT NULL,             -- lowercased, whitespace-collapsed
    query_token_count       INT NOT NULL,              -- proxy for "name" vs "question"
    result_count_vector     INT NOT NULL,              -- passed SIMILARITY_THRESHOLD
    result_count_total      INT NOT NULL,              -- after web-fallback dedup
    fallback_used           BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms_total        INT NOT NULL,
    latency_ms_embed        INT,
    latency_ms_vector       INT,
    latency_ms_fallback     INT,                       -- null when fallback not used
    session_id              TEXT,                      -- localStorage UUID
    ip_hash                 TEXT,                      -- HMAC-SHA256, never raw
    user_agent_class        TEXT,                      -- mobile|desktop|bot|unknown
    matched_entity_type     TEXT,                      -- Phase 2 hook: politician|org|bill|outlet
    matched_entity_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_queries_created
    ON search_queries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_queries_query_norm
    ON search_queries(query_norm);

-- Primer-expand instrumentation (migrations/010_primer_expand_events.sql).
-- Phase 1 question: do users actually open the "What you should know first"
-- panel? Without this signal, all primer-content iteration is guessing.
-- Tracks expand clicks only (not impressions — those are computable from
-- articles.context_primer IS NOT NULL without paying for the writes).
-- IPs hashed; 90-day retention via scripts/cleanup_old_primer_events.py.
CREATE TABLE IF NOT EXISTS primer_expand_events (
    id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    article_id              TEXT,             -- nullable: future surfaces may lack one
    surface                 TEXT,             -- 'feed' | 'bookmarks' | future
    session_id              TEXT,             -- localStorage UUID
    ip_hash                 TEXT,             -- HMAC-SHA256, never raw
    user_agent_class        TEXT              -- mobile|desktop|bot|unknown
);

CREATE INDEX IF NOT EXISTS idx_primer_expand_events_created
    ON primer_expand_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_primer_expand_events_article
    ON primer_expand_events(article_id) WHERE article_id IS NOT NULL;

-- Daily AI cost ledger (migrations/011_ai_usage_daily.sql).
-- One row per (UTC date, provider, model, operation). services/cost_guard.py
-- sums the day's estimated_cost_usd to enforce a hard daily ceiling on the
-- live paid paths (compare web-search + Voyage embeddings) and to alert at 80%
-- of budget (sift-api#70). Frontend topic-search paid calls are NOT covered
-- here yet — they remain a temporary D35 exception until sift-api#79 moves
-- that fallback into sift-api.
-- Token columns (migrations/029) exist so spend can be re-priced against a
-- different model's rates. A stored dollar figure cannot be: cost is one
-- equation with two unknowns, so "what would this stage cost on model X" is
-- unanswerable from dollars alone. The stages sit at opposite ends of the
-- input:output ratio and Haiku prices output at 5x input, so the answer differs
-- per stage rather than scaling uniformly.
CREATE TABLE IF NOT EXISTS ai_usage_daily (
    usage_date          DATE NOT NULL,
    provider            TEXT NOT NULL,          -- 'anthropic' | 'voyage'
    model               TEXT NOT NULL,
    operation           TEXT NOT NULL,          -- call-site id, e.g. 'compare.search'
    estimated_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    call_count          INTEGER NOT NULL DEFAULT 0,
    input_tokens        BIGINT NOT NULL DEFAULT 0,
    output_tokens       BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens   BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens  BIGINT NOT NULL DEFAULT 0,
    web_search_calls    INTEGER NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, provider, model, operation)
);

-- How each Claude call ended, per day (migrations/021 + 029).
-- Was only ever in app/db.py:_apply_migrations, so a fresh DB built from this
-- file alone did not have it — added here 2026-08-12 to close that drift.
--
-- Splitting on `aligned` is the point: the question is not "do we ever hit the
-- cap" but "are the misaligned ones the ones that did". `model` is in the key
-- (029) because during a model A/B both arms otherwise collide on one row, and
-- this split is the only stored signal for whether a model can produce
-- parseable indexed JSON.
CREATE TABLE IF NOT EXISTS llm_output_stops (
    usage_date        DATE    NOT NULL,
    operation         TEXT    NOT NULL,
    model             TEXT    NOT NULL DEFAULT '',
    stop_reason       TEXT    NOT NULL,
    aligned           BOOLEAN NOT NULL,
    -- Kept in the key so the data stays readable across a BATCH_SIZE change —
    -- which is the decision this table exists to inform.
    batch_size        INTEGER NOT NULL,
    call_count        INTEGER NOT NULL DEFAULT 0,
    -- High-water mark. Against the call's max_tokens this is the headroom
    -- reading, and it stays meaningful even if nothing ever truncates.
    max_output_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, operation, model, stop_reason, aligned, batch_size)
);

-- Curated outlet provenance (migrations/006).
-- Ownership, funding model and third-party ratings for the outlets Sift
-- ingests from, rendered on /outlet/[slug]. Sift asserts no rating of its
-- own: it reproduces AllSides' political lean and MBFC's factual rating
-- verbatim, each with its source URL and the date a human last checked it,
-- applied symmetrically across the spectrum (methodology at /methodology).
-- An outlet with no row here simply renders without the provenance
-- affordance — graceful degradation, not an error.
-- Populated from data/outlet_profiles.csv by scripts/seed_outlet_profiles.py.
CREATE TABLE IF NOT EXISTS outlet_profiles (
    slug                  TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    parent_company        TEXT,
    parent_company_url    TEXT,
    founded_year          INT,
    funding_model         TEXT,             -- 'subscription'|'advertising'|'foundation'|'donations'|'mixed'|'public-service'
    major_funders         JSONB DEFAULT '[]'::jsonb,
    allsides_rating       TEXT,             -- 'left'|'lean-left'|'center'|'lean-right'|'right'|'mixed'
    allsides_url          TEXT,             -- required for allsides_rating to render
    allsides_last_checked DATE,
    mbfc_factual          TEXT,             -- 'high'|'mostly-factual'|'mixed'|'low'|'very-low'
    mbfc_url              TEXT,             -- required for mbfc_factual to render
    mbfc_last_checked     DATE,
    notes                 TEXT,             -- reviewer prose
    external_links        JSONB DEFAULT '{}'::jsonb,
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outlet_profiles_name_lower
    ON outlet_profiles (LOWER(name));

-- The other half of migration 006: RSS hands us messy free-text source_name
-- values ("Reuters", "Reuters.com", "Reuters | Breaking news worldwide") that
-- have to collapse onto one outlet_slug before an article can show provenance.
-- An unmapped source_name is not an error — the article just renders without
-- the affordance. scripts/audit_source_aliases.py suggests new rows from
-- unmatched articles.source_name values; scripts/seed_source_aliases.py
-- applies the reviewed CSV. Also read by workflows/compare_workflow.py to keep
-- the compare source pool in sync.
CREATE TABLE IF NOT EXISTS source_name_aliases (
    raw_source_name TEXT PRIMARY KEY,
    outlet_slug     TEXT NOT NULL REFERENCES outlet_profiles (slug) ON DELETE CASCADE,
    added_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_name_aliases_slug
    ON source_name_aliases (outlet_slug);

-- Curated politician / official dossiers (migrations/007, 015).
--
-- The PK holds a Congress.gov bioguide ID for sitting and former members, and
-- a synthetic Sift id ('EXEC-TRUMP-DJ', 'FOREIGN-PUTIN-V', 'SCOTUS-ROBERTS-J')
-- for executive-branch, foreign and Supreme Court officials, who have none.
-- id_source records which. See migrations/015_politician_role_provenance.sql
-- for why the column was not renamed, and migrations/016 for why Justices live
-- here rather than in a table of their own.
--
-- Every claim-bearing role column is paired with the source column that backs
-- it, on migration 013's annual_budget_usd/_fy/_source pattern: a value cannot
-- render without its primary record. OPERATING_CONTEXT.md §5 forbids a dossier
-- claim about a living person without a citation to that record, and these
-- pages are profile-only — there is no article list to carry the page if the
-- fields are unsourced.
CREATE TABLE IF NOT EXISTS politician_profiles (
    bioguide_id                     TEXT PRIMARY KEY,
    name                            TEXT NOT NULL,
    party                           TEXT,             -- 'D' | 'R' | 'I' | national party code
    state                           TEXT,             -- USPS code, or ISO-3166 for foreign rows
    chamber                         TEXT,             -- 'senate'|'house'|'former'|'executive'|'foreign-executive'|'scotus'
    committees                      JSONB DEFAULT '[]'::jsonb,
    top_industries_current_cycle    JSONB DEFAULT '[]'::jsonb,
    -- Array of third-party scorecard entries, each carrying its own
    -- year and source_url. See migrations/019.
    interest_group_ratings          JSONB DEFAULT '[]'::jsonb,
    external_links                  JSONB DEFAULT '{}'::jsonb,
    notes                           TEXT,             -- reviewer prose; NULL on executive rows (015)
    id_source                       TEXT,             -- 'bioguide'|'executive'|'foreign-executive'|'scotus'
    role_title                      TEXT,             -- verbatim office title
    role_title_source               TEXT,             -- statute / constitutional provision / official site
    role_start_date                 DATE,
    role_end_date                   DATE,
    role_dates_source               TEXT,
    nomination_date                 DATE,
    nomination_url                  TEXT,             -- congress.gov PN record; also sources predecessor_name
    confirmation_date               DATE,
    confirmation_vote_url           TEXT,             -- senate.gov roll-call; also sources the result
    confirmation_vote_result        TEXT,             -- verbatim, e.g. 'Confirmed 50-50'
    predecessor_name                TEXT,             -- previous holder of the office
    predecessor_source              TEXT,
    role_verified_at                DATE,             -- last refetch of role_title_source; expires foreign rows (017)             -- congress.gov PN ("vice <name>" clause) or the prior roll-call
    refreshed_at                    TIMESTAMPTZ DEFAULT NOW(),
    updated_at                      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_politician_profiles_name_lower
    ON politician_profiles (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_politician_profiles_state_party
    ON politician_profiles (state, party);
CREATE INDEX IF NOT EXISTS idx_politician_profiles_chamber
    ON politician_profiles (chamber);
-- Matches the publish gate in sift/lib/db.ts listSitemapEntries().
CREATE INDEX IF NOT EXISTS idx_politician_profiles_role_verified
    ON politician_profiles (chamber, role_verified_at)
    WHERE role_title IS NOT NULL AND role_title_source IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_politician_profiles_sourced_role
    ON politician_profiles (chamber)
    WHERE role_title IS NOT NULL AND role_title_source IS NOT NULL;

-- Curated org dossiers — think tanks, advocacy groups, agencies
-- (migrations/007, 012, 013).
--
-- There is deliberately no political_lean column. Migration 012 deprecated it
-- and 013 dropped it: it was hand-authored in data/org_profiles.csv and
-- rendered on /org/[slug] as a bare assertion with no source, date or method,
-- which is Sift computing its own political rating — forbidden by D37. Adding
-- it back to a fresh DB would re-open that hole, so it is absent here by
-- design, not by omission.
--
-- What replaced it is the organization's own words, quoted verbatim and
-- linked. Each claim-bearing column is paired with the source that backs it,
-- and sift/lib/org.ts nulls the pair when the source is missing, so an
-- uncited characterization of a real organization cannot reach a page.
-- Populated from data/org_profiles.csv by scripts/seed_org_profiles.py.
CREATE TABLE IF NOT EXISTS org_profiles (
    slug                     TEXT PRIMARY KEY,  -- e.g. 'brookings-institution'
    name                     TEXT NOT NULL,
    type                     TEXT,              -- 'think-tank'|'advocacy'|'union'|'pac'|'super-pac'|'foundation'|'industry-group'|'agency'|'other'
    founded_year             INT,
    self_description         TEXT,              -- the org's own characterization, verbatim — never Sift's
    self_description_source  TEXT,              -- URL the quote came from; required to render
    self_description_checked DATE,              -- when a human last verified the quote
    governance_structure     TEXT,              -- agencies only; statutory facts that don't rot
    governance_source        TEXT,              -- URL (US Code, agency site); required to render
    annual_budget_usd        NUMERIC,           -- total functional expenses from ONE Form 990, not a general "budget"
    annual_budget_fy         TEXT,              -- fiscal year as the filing states it, e.g. 'FY ending December 2024'
    annual_budget_source     TEXT,              -- that specific filing page; required to render
    major_funders            JSONB DEFAULT '[]'::jsonb,
    fara_registered          BOOLEAN DEFAULT FALSE,
    fara_countries           JSONB DEFAULT '[]'::jsonb,
    external_links           JSONB DEFAULT '{}'::jsonb,  -- { propublica, irs_990, fara, wikipedia, official }
    notes                    TEXT,              -- reviewer prose
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_profiles_name_lower
    ON org_profiles (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_org_profiles_type
    ON org_profiles (type);

-- Curated bill dossiers (migrations/007).
-- Populated on demand when an article references a bill not yet in the table.
-- Every figure here is a public-record number surfaced with attribution via
-- external_links, on the same discipline as the other dossier tables.
CREATE TABLE IF NOT EXISTS bill_profiles (
    bill_id              TEXT PRIMARY KEY,  -- chamber-number-congress, e.g. 's-1234-119'
    congress             INT NOT NULL,      -- e.g. 119 for the 119th Congress
    title                TEXT NOT NULL,     -- official title from Congress.gov
    short_title          TEXT,              -- popular name, e.g. 'Inflation Reduction Act'
    sponsor_bioguide     TEXT REFERENCES politician_profiles (bioguide_id) ON DELETE SET NULL,
    cosponsors           JSONB DEFAULT '[]'::jsonb,   -- bioguide IDs
    status               TEXT,              -- 'introduced'|'committee'|'passed-chamber'|'enacted'|'vetoed'|'failed'
    introduced_date      DATE,
    lobbying_for_usd     NUMERIC,           -- aggregate from OpenSecrets
    lobbying_against_usd NUMERIC,
    external_links       JSONB DEFAULT '{}'::jsonb,   -- { govtrack, opensecrets, congress }
    notes                TEXT,              -- reviewer prose
    refreshed_at         TIMESTAMPTZ DEFAULT NOW(),   -- last automated-refresh time
    updated_at           TIMESTAMPTZ DEFAULT NOW()    -- last human-edit time
);

CREATE INDEX IF NOT EXISTS idx_bill_profiles_sponsor
    ON bill_profiles (sponsor_bioguide);
CREATE INDEX IF NOT EXISTS idx_bill_profiles_congress
    ON bill_profiles (congress);
CREATE INDEX IF NOT EXISTS idx_bill_profiles_short_title_lower
    ON bill_profiles (LOWER(short_title));

-- Curated surface-form → dossier map for the entity linker (migrations/014).
-- services/entity_linker.py matches full canonical names only, which is the
-- right default — mechanically-derived last-name aliases were removed in #40
-- because common-noun surnames (Cloud, Self, Banks, Hill) false-match
-- constantly. Derived aliases are unsafe; curated ones are the alternative,
-- and they are most of the miss: "Pentagon" appeared in 756 articles without
-- a chip while united-states-department-of-defense already existed.
-- Shape follows source_name_aliases, which does the same job for RSS
-- source_name strings. Populated from data/entity_aliases.csv by
-- scripts/seed_entity_aliases.py.
CREATE TABLE IF NOT EXISTS entity_aliases (
    -- Lowercased surface form, globally unique on purpose: build_search_dict
    -- drops any key with conflicting refs anyway, so an alias that could mean
    -- two things is worthless. The PK surfaces the conflict at curation time
    -- rather than silently at link time.
    alias        TEXT PRIMARY KEY,
    entity_type  TEXT NOT NULL
                 CHECK (entity_type IN ('politician', 'org', 'bill', 'outlet')),
    -- No FK: the four dossier tables have four different PK columns, so this
    -- cannot be a single reference. seed_entity_aliases.py validates against
    -- the live tables and refuses an unresolvable target.
    canonical_id TEXT NOT NULL,
    notes        TEXT,              -- justification; an alias is a claim that two names denote one entity
    -- Migration 017. When true the linker matches this alias case-sensitively
    -- and `alias` above holds the exact casing to match ("ICE"), rather than
    -- the lowercased form every other row stores. For acronyms whose lowercase
    -- is an ordinary word: `ice` is ice cream and sea ice in 672 articles,
    -- `ICE` is the agency in 1,725.
    match_case   BOOLEAN NOT NULL DEFAULT FALSE,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_target
    ON entity_aliases (entity_type, canonical_id);

-- Feed-balance drift snapshots (migrations/022, ranking v2 stage 3).
-- One row per category per daily check, written by services/feed_balance.py.
-- grim_share_top10 / mean_civic_top10 are the tripped metrics (the D48/D45
-- policy numbers); story columns record the stage-1 saturation change.
-- Persisted rather than only logged so the trailing-13-day baselines
-- survive Railway log rotation and deploys.
CREATE TABLE IF NOT EXISTS feed_balance (
    run_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category              TEXT NOT NULL,
    grim_share_top10      REAL,
    mean_civic_top10      REAL,
    mean_sources_top5     REAL,
    story_grim_share_top5 REAL,
    n_articles            INTEGER NOT NULL DEFAULT 0,
    n_stories             INTEGER NOT NULL DEFAULT 0,
    opinion_share_top10   REAL,  -- migrations/023; recorded, untripped
    PRIMARY KEY (run_at, category)
);

CREATE INDEX IF NOT EXISTS idx_feed_balance_run_at
    ON feed_balance (run_at DESC);

-- Curated term definitions. See migrations/031_term_profiles.sql for why the
-- unsourced primer definitions are not enough.
CREATE TABLE IF NOT EXISTS term_profiles (
    slug               TEXT PRIMARY KEY,
    term               TEXT NOT NULL,
    definition         TEXT NOT NULL,
    definition_source  TEXT NOT NULL,
    definition_checked DATE,
    aliases            JSONB NOT NULL DEFAULT '[]'::jsonb,
    category           TEXT,
    notes              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_term_profiles_term_lower
    ON term_profiles (LOWER(term));

-- Prefilter for the /term/<slug> coverage query. See
-- migrations/032_articles_fulltext.sql — the read query keeps an exact
-- word-boundary regex alongside this, because FTS stems and would otherwise
-- widen what the page claims to cover.
CREATE INDEX IF NOT EXISTS idx_articles_fulltext
    ON articles USING gin (to_tsvector('english',
        COALESCE(title, '') || ' ' || COALESCE(summary, '')));

-- Primer-term keys + index. See migrations/033_primer_term_index.sql — lets
-- the /term/<slug> coverage query count articles whose primer defines a term,
-- not only those with it in the headline. Coverage signal only: every primer
-- term in the corpus has source: null.
CREATE OR REPLACE FUNCTION primer_term_keys(p jsonb)
RETURNS text[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
  SELECT COALESCE(array_agg(DISTINCT lower(btrim(x->>'term'))), ARRAY[]::text[])
    FROM jsonb_array_elements(
           CASE WHEN jsonb_typeof(p->'terms') = 'array' THEN p->'terms' ELSE '[]'::jsonb END
         ) x
   WHERE jsonb_typeof(x) = 'object'
     AND NULLIF(btrim(x->>'term'), '') IS NOT NULL
$fn$;

CREATE INDEX IF NOT EXISTS idx_articles_primer_terms
    ON articles USING gin (primer_term_keys(context_primer));
