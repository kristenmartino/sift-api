-- Per-day counts of how each Claude call *ended*, by operation.
--
-- WHY THIS EXISTS
-- ---------------
-- `summarizer.batch` re-asks a batch whose response cannot be proven to line
-- up with its input (services/index_alignment). Measured 2026-08-11, those
-- re-asks run at 4-12% of calls — 25 extra calls on 2026-08-05, 23 on 08-11 —
-- and every one is a full-price duplicate. Nothing records *why* they
-- misalign.
--
-- One hypothesis is cheap to test and cheap to fix: `max_tokens = 700` against
-- five summaries at ~60 tokens each leaves real headroom on a typical batch,
-- but the longest summaries in prod run 79 words (~105 tokens), so five long
-- ones land near 575 plus JSON scaffolding. A response cut off at the cap is
-- truncated JSON, and truncated JSON fails alignment exactly the way these
-- retries look. If that is what is happening, raising the cap *removes* cost
-- instead of adding it.
--
-- `stop_reason` answers it directly, and the answer has to be queryable: the
-- same reasoning as 018_threading_shadow — the Railway log buffer rotates and
-- resets on deploy, so a 24h aggregate cannot be reconstructed from it.
--
-- Deliberately an aggregate, not a row per call: a handful of rows per day,
-- no growth problem, and no article text.
CREATE TABLE IF NOT EXISTS llm_output_stops (
    usage_date        DATE    NOT NULL,
    operation         TEXT    NOT NULL,
    stop_reason       TEXT    NOT NULL,
    -- FALSE only for a response that failed index alignment. Splitting on it
    -- is the whole point: the question is not "do we ever hit the cap" but
    -- "are the misaligned ones the ones that hit the cap".
    aligned           BOOLEAN NOT NULL,
    -- Kept in the key so the data stays readable across a BATCH_SIZE change —
    -- which is the decision this table exists to inform.
    batch_size        INTEGER NOT NULL,
    call_count        INTEGER NOT NULL DEFAULT 0,
    -- High-water mark. Against the call's max_tokens this is the headroom
    -- reading, and it stays meaningful even if nothing ever truncates.
    max_output_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, operation, stop_reason, aligned, batch_size)
);
