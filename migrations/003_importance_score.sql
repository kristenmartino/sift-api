-- Add AI-generated importance score (1-5) to articles
-- Used for smart feed ranking: importance * recency_decay

ALTER TABLE articles ADD COLUMN IF NOT EXISTS importance_score INTEGER;
