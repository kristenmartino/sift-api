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
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_articles_category_date
    ON articles(category, published_date DESC);

-- Note: IVFFlat index requires rows to exist for training.
-- Run after initial data load:
-- CREATE INDEX idx_articles_embedding ON articles
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);

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

-- Bookmarks: user-saved articles
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, article_id)
);

CREATE INDEX IF NOT EXISTS idx_bookmarks_user
    ON bookmarks(user_id, created_at DESC);

-- Pipeline state: tracks last refresh per category
CREATE TABLE IF NOT EXISTS pipeline_state (
    category TEXT PRIMARY KEY,
    last_refreshed_at TIMESTAMPTZ,
    article_count INTEGER DEFAULT 0,
    error TEXT
);

-- Seed pipeline_state with all 7 categories
INSERT INTO pipeline_state (category) VALUES
    ('top'), ('technology'), ('business'), ('science'),
    ('energy'), ('world'), ('health')
ON CONFLICT (category) DO NOTHING;
