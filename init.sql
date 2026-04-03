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
    from_search BOOLEAN NOT NULL DEFAULT false, -- TODO: set true when compare-discovered articles are persisted
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_articles_category_date
    ON articles(category, published_date DESC);

-- Note: IVFFlat index requires rows to exist for training.
-- Run after initial data load:
-- CREATE INDEX idx_articles_embedding ON articles
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);

-- TODO: Custom topics and bookmarks tables below are consumed by the
-- Next.js frontend via Neon/Supabase client. API endpoints for these
-- are planned for a future release.

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
