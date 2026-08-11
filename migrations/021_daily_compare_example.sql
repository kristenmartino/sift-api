-- Daily compare example — the anonymous door into the compare feature.
--
-- One row, refreshed at most once per UTC day by the pipeline (see
-- services/daily_compare.py). The frontend serves it to signed-out visitors
-- on /news compare mode and to the landing page's comparison section, which
-- until now showed a hand-written static example. Storing one real, dated
-- comparison keeps the marketing surface honest ("this is what the tool made
-- this morning") at a bounded cost of ~one compare per day, inside the same
-- daily AI budget guard as the live endpoint.
--
-- payload is the CompareResponse shape verbatim:
--   { topic, comparison, sources_checked, claims, duration_ms }
--
-- Applied in prod by app/db.py:_apply_migrations at startup; this file is
-- documentation + manual ops.

CREATE TABLE IF NOT EXISTS daily_compare_example (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    payload JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
