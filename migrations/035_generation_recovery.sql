-- Durable source evidence and retry queue; unknown legacy copy stays unversioned.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS generation_raw_content TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS generation_status TEXT NOT NULL DEFAULT 'complete';
ALTER TABLE articles ADD COLUMN IF NOT EXISTS generation_attempted_at TIMESTAMPTZ;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS copy_rules_version TEXT;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS copy_rules_version TEXT;
CREATE INDEX IF NOT EXISTS idx_articles_generation_pending
    ON articles (generation_attempted_at NULLS FIRST, created_at)
    WHERE generation_status = 'pending' AND from_search = false;
