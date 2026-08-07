-- 017_articles_threaded_at.sql
--
-- Marks an article as having been through story threading, so threading can
-- consume a queue instead of rescanning a window.
--
-- WHY A PER-ROW MARKER AND NOT A TIMESTAMP WATERMARK
-- --------------------------------------------------
-- docs/INCREMENTAL_THREADING.md proposed a `created_at` watermark in
-- pipeline_state. That has a data-loss hole, measured 2026-08-05: entities are
-- populated asynchronously by the batch poller, and workflows/story_workflow.py
-- requires `jsonb_typeof(entities) = 'object'`. 1.58% of articles are still
-- pending at any moment (avg lag 3.9 min). A watermark advances past them while
-- they are ineligible, and they are never reconsidered — ~32 articles/day
-- dropped permanently, compounding.
--
-- The current rescan design accidentally protects against that, because it
-- re-reads the whole window every run. Replacing it with a watermark would
-- remove the protection. A per-row marker keeps it: an article whose entities
-- have not landed simply stays NULL and is picked up whenever they do. There is
-- no lag hole to design around, and no catch-up sweep to remember to run.
--
-- NO BACKFILL NEEDED. Every existing row is NULL, but the queue query also
-- bounds on `published_date > NOW() - INTERVAL '48 hours'`, so the ~280k
-- historical rows fall outside the window and are never selected. They age
-- further out, never in. Backfilling them would rewrite 280k rows and — since
-- every index entry has to be rewritten with them — cost ~60 MB of index bloat
-- for no behavioural difference (see docs/NEON_RETENTION.md).
--
-- NO NEW INDEX. The working set is the 48h window, ~4,400 rows across all
-- categories. idx_articles_category_date already serves the
-- (category, published_date) part; `threaded_at IS NULL` is a cheap filter on
-- top of that. A partial index on `threaded_at IS NULL` would instead span
-- every historical row, which is the opposite of useful.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS threaded_at TIMESTAMPTZ;

COMMENT ON COLUMN articles.threaded_at IS
  'When story threading last considered this article. NULL = queued. Set '
  'regardless of outcome, including for articles that matched nothing — a '
  'parked singleton stays searchable as a kNN neighbour, so a later arrival '
  'can still pull it into a story without it being re-queued.';
