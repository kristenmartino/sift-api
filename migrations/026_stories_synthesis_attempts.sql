-- Retry counter for the failed-story sweeper (#211).
--
-- `synthesis_status='failed'` means a story holds a degraded placeholder and
-- still owes a real synthesis. Until now nothing acted on that: the only
-- reader was `workflows/story_workflow.py:226`, and `pipeline_workflow.py:459`
-- makes the two threading paths mutually exclusive, so with incremental
-- threading enabled that code never runs. A 'failed' row was invisible to the
-- feed (`idx_stories_feed`) and never revisited.
--
-- The sweeper in `run_incremental_threading` retries them. This column bounds
-- that: a story whose synthesis fails for a structural reason — rather than a
-- transient one — must not be re-paid for on every 30-minute run, forever.
-- Counting attempts is what makes "give up after N" expressible; an age cut-off
-- alone would still burn N-per-run until the age passed.
--
-- Additive with a default, so existing rows start at 0 and are all eligible.
ALTER TABLE stories ADD COLUMN IF NOT EXISTS synthesis_attempts INTEGER DEFAULT 0;

-- Partial index over exactly the sweeper's predicate. The table is small today
-- (~1k rows) but this is a per-run query on the live pipeline path, and the
-- population it selects is a handful of rows out of the whole table.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stories_failed_sweep
    ON stories (synthesis_attempts, created_at DESC)
    WHERE synthesis_status = 'failed';
