-- Migration 001: Story Threading
-- Groups articles covering the same event into stories with AI synthesis.

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

-- Link articles to their parent story (NULL = standalone)
ALTER TABLE articles ADD COLUMN IF NOT EXISTS story_id TEXT REFERENCES stories(id);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS entities JSONB DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_articles_story_id ON articles(story_id);
