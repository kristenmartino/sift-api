-- 018_threading_shadow.sql
--
-- One row per shadow run of incremental threading.
--
-- WHY THIS EXISTS
-- ---------------
-- The shadow report is the evidence that decides whether to cut over from the
-- rescan path to incremental threading (docs/INCREMENTAL_THREADING.md). Until
-- now it existed only as a log line, and the Railway log buffer rotates and
-- resets on every deploy — the pre-deploy history was already lost once on
-- 2026-08-07. A 24-hour aggregate cannot be reconstructed from a buffer that
-- does not keep 24 hours.
--
-- This is the same failure `ai_usage_daily` had: the cost figure in STATUS.md
-- sat 20x wrong for weeks because the number nobody could query was the number
-- nobody checked. A number that gates a decision has to live somewhere you can
-- SELECT it from.
--
-- ~48 rows/day. No retention policy: at that rate a year is ~17k rows, which
-- is not worth a cleanup script.

CREATE TABLE IF NOT EXISTS threading_shadow (
    run_at                            TIMESTAMPTZ PRIMARY KEY DEFAULT NOW(),

    -- Articles waiting in the real queue. Reported, not analysed: the queue is
    -- oldest-first and never drains while the flag is off, so measuring it
    -- would re-decide one stale slice per run at ~10x cost and bias the answer
    -- favourably. See services/story_matcher.fetch_recent_sample.
    backlog                           INTEGER NOT NULL,
    -- Newest-first sample actually analysed — the steady-state proxy.
    sampled                           INTEGER NOT NULL,

    attach_candidates                 INTEGER NOT NULL,
    new_cluster_candidates            INTEGER NOT NULL,
    -- Of those, how many carry >= 2 unique outlets and would survive the gate.
    new_clusters_passing_outlet_gate  INTEGER NOT NULL,
    parked                            INTEGER NOT NULL,
    -- Parked articles with a neighbour in [near_miss_floor, threshold). A
    -- persistently high share means the threshold may be too strict — the one
    -- question its own calibration cannot answer, since those 283 pairs came
    -- from a ~3.3h window.
    parked_with_near_miss             INTEGER NOT NULL,
    llm_relevant                      INTEGER NOT NULL,

    threshold                         REAL,
    near_miss_floor                   REAL,
    llm_relevant_by_category          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- NULL unless incremental_threading_confirm_dryrun is on. would_group is
    -- the number the cutover bar compares against the live path's grouped
    -- count; everything above it is an upper bound, not a prediction.
    dry_run                           JSONB,
    would_group                       INTEGER,
    confirm_rate                      REAL
);

CREATE INDEX IF NOT EXISTS idx_threading_shadow_run_at
  ON threading_shadow (run_at DESC);
