-- 004_feed_indexes.sql
--
-- Partial indexes that match the exact predicates used by the user-facing
-- `/api/news?category=...` queries in sift/lib/db.ts. Without these, the
-- planner reverts to sequential scans on `articles` (filtered by
-- from_search + summary quality), which pushes business/sports past the
-- 10s client-side abort (API_TIMEOUT_MS in sift/lib/constants.ts).
--
-- CONCURRENTLY lets us add these on a live production DB without blocking
-- writes. IF NOT EXISTS keeps the file idempotent for re-runs.
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
-- Apply this file with `psql -f` (one statement at a time, autocommit),
-- NOT wrapped in BEGIN/COMMIT.

-- 1. Feed-quality articles by category + recency.
--    Serves getArticlesByCategory (lib/db.ts:36) and the standalone-articles
--    query in getStoriesWithArticles (lib/db.ts:150).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_feed
    ON articles (category, published_date DESC)
    WHERE from_search = false
      AND summary IS NOT NULL
      AND summary <> '';

-- 2. Feed-quality articles by story_id.
--    Serves the LEFT JOIN in the stories query (lib/db.ts:85) and the
--    `story_id = ANY($1)` fetch (lib/db.ts:121).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_story_feed
    ON articles (story_id)
    WHERE story_id IS NOT NULL
      AND from_search = false
      AND summary IS NOT NULL
      AND summary <> '';

-- 3. Complete stories by category + recency.
--    Serves the outer filter in getStoriesWithArticles (lib/db.ts:85).
--    Narrows to rows the UI actually renders (synthesis_status = 'complete').
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stories_feed
    ON stories (category, published_date DESC)
    WHERE synthesis_status = 'complete';
