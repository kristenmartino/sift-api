-- Serve the outlet dossier's recent-articles query from an index.
--
-- getRecentArticlesByOutletSlug (sift/lib/db.ts) was the single most expensive
-- statement in the database: 19.5% of all tracked execution time, 900ms mean,
-- 53.5% buffer hit ratio (pg_stat_statements, 2026-08-14). It ran per outlet
-- dossier page view.
--
-- Nothing could serve it:
--
--   WHERE LOWER(source_name) IN (SELECT sn FROM outlet_source_names)
--     AND from_search = false
--     AND summary IS NOT NULL AND summary <> ''
--   ORDER BY COALESCE(published_date, created_at) DESC NULLS LAST
--   LIMIT $2
--
--   * LOWER(source_name) is an expression; no expression index existed, so the
--     filter could not use idx_articles_category_date or anything else.
--   * ORDER BY COALESCE(published_date, created_at) is also an expression. The
--     feed queries deliberately avoid COALESCE for exactly this reason — see
--     the comment above idx_articles_feed in sift/lib/db.ts, which explains
--     that the recency floor is written as an OR so both branches stay
--     index-servable. This query predates that lesson being written down.
--
-- So Postgres scanned all ~283k rows and sorted them, to return 20.
--
-- This index matches the filter AND the sort, in that order, with the same
-- partial predicate the query carries — so the plan becomes an index scan that
-- stops at LIMIT, with no sort node at all.
--
-- Note this is the same class of defect the 30-day recency floor fixed in
-- sift#172, reappearing in a query that was never thought of as a "feed"
-- query. The floor was applied to feeds; dossiers were missed.
--
-- Same DDL applied at startup by app/db.py:_apply_migrations (the prod apply
-- path on Railway), minus CONCURRENTLY, which cannot run inside a transaction
-- and is unnecessary for a fresh database. Use this file when applying by hand
-- against a live one: the table is ~283k rows and a plain CREATE INDEX would
-- hold a write lock for the duration.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_outlet_recent
    ON articles (LOWER(source_name), COALESCE(published_date, created_at) DESC)
    WHERE from_search = false
      AND summary IS NOT NULL
      AND summary <> '';
