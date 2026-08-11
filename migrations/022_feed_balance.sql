-- Feed-balance drift snapshots (ranking v2 stage 3, sift/docs/RANKING_SIGNALS.md).
-- One row per category per daily check, written by services/feed_balance.py.
-- Persisted (not just logged) for the same reason threading_shadow is:
-- Railway's log buffer rotates and resets on deploy, so the trailing-13-day
-- baselines the tripwire compares against could not be reconstructed from logs.
--
-- grim_share_top10 / mean_civic_top10 are the tripped metrics (the two
-- policy-bearing numbers of D48 + D45); the story columns record the stage-1
-- saturation change without a tripwire.
--
-- Same DDL applied at startup by app/db.py:_apply_migrations (the prod
-- apply path on Railway); this file is documentation + manual ops.

CREATE TABLE IF NOT EXISTS feed_balance (
    run_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category              TEXT NOT NULL,
    grim_share_top10      REAL,
    mean_civic_top10      REAL,
    mean_sources_top5     REAL,
    story_grim_share_top5 REAL,
    n_articles            INTEGER NOT NULL DEFAULT 0,
    n_stories             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_at, category)
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_feed_balance_run_at
    ON feed_balance (run_at DESC);
