-- 029_ai_usage_tokens.sql
-- Per-model token accounting on the cost ledger.
--
-- WHY THIS EXISTS
-- ---------------
-- `ai_usage_daily` records dollars and call counts, and nothing else. That is
-- enough to answer "what did we spend" and not enough to answer "what would
-- this stage cost on a different model" — which is the question a model swap
-- opens. Cost is one equation with two unknowns:
--
--     cost = input_tokens * price_in + output_tokens * price_out
--
-- so a stored dollar figure cannot be re-priced against another model's rates.
-- Today you can only find out by spending. That is fine while there is one
-- model; it is the blocker the moment there are two, because the stages sit at
-- opposite ends of the input:output ratio — `entity_linker_llm.link_text` is
-- input-heavy (roster + article in, <=500 tokens out), `story_synthesizer` is
-- output-heavy — and Haiku prices output at 5x input. A candidate that looks
-- 3x cheaper per token can be more expensive on half the pipeline.
--
-- Columns rather than a side table: the existing primary key
-- (usage_date, provider, model, operation) is already exactly this grain, and
-- a side table would mean a second write on the hot path plus a join in
-- `check_budget` — whose read sits on the fail-closed path, where a failure
-- blocks every paid call. Additive columns leave that query untouched.
--
-- BIGINT because ~2,000 articles/day across ~10 stages already reaches tens of
-- millions of input tokens per day, and docs/SOURCE_SCALING.md models a +1000
-- outlet scenario at ~20x that.
--
-- On PG11+ adding NOT NULL with a constant DEFAULT is metadata-only — no table
-- rewrite, no lock held for the scan.
--
-- Applied at startup (idempotent) by app/db.py:_apply_migrations. This file is
-- the manual-ops / documentation copy.

ALTER TABLE ai_usage_daily
    ADD COLUMN IF NOT EXISTS input_tokens       BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE ai_usage_daily
    ADD COLUMN IF NOT EXISTS output_tokens      BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE ai_usage_daily
    ADD COLUMN IF NOT EXISTS cache_read_tokens  BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE ai_usage_daily
    ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT  NOT NULL DEFAULT 0;
ALTER TABLE ai_usage_daily
    ADD COLUMN IF NOT EXISTS web_search_calls   INTEGER NOT NULL DEFAULT 0;

-- `llm_output_stops` needs `model` in its key for the same reason.
--
-- The table splits calls on `aligned` because the question it exists to answer
-- is not "do we ever hit the cap" but "are the misaligned ones the ones that
-- did" (migrations/021). During an A/B that split is the only stored signal for
-- whether a candidate model can produce parseable indexed JSON at all — and
-- without `model` in the primary key both arms collide on the same row and pool
-- into one unreadable number, exactly when it matters most.
--
-- ~10 rows/day and no readers outside ad-hoc SQL, so the rewrite is free.
-- Existing rows are all Haiku; they backfill to '' rather than to a model name
-- because inventing provenance for data that never recorded it is worse than
-- leaving it blank.

ALTER TABLE llm_output_stops
    ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

-- Idempotent: re-adding a primary key errors, so only rebuild it when `model`
-- is not already part of the existing one.
DO $$
BEGIN
    IF to_regclass('llm_output_stops') IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM pg_index i
        JOIN pg_attribute a
          ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = 'llm_output_stops'::regclass
          AND i.indisprimary
          AND a.attname = 'model'
    ) THEN
        ALTER TABLE llm_output_stops DROP CONSTRAINT IF EXISTS llm_output_stops_pkey;
        ALTER TABLE llm_output_stops ADD PRIMARY KEY
            (usage_date, operation, model, stop_reason, aligned, batch_size);
    END IF;
END $$;
