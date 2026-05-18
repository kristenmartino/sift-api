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
    why_it_matters TEXT,
    importance_score INTEGER,
    content_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

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
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stories_category_date
    ON stories(category, published_date DESC);

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
