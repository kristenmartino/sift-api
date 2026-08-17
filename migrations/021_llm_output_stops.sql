-- Per-day counts of how each Claude call *ended*, by operation.
--
-- WHY THIS EXISTS
-- ---------------
-- `summarizer.batch` re-asks a batch whose response cannot be proven to line
-- up with its input (services/index_alignment). This table was built to test
-- whether those re-asks were caused by output truncation: `max_tokens = 700`
-- against five ~60-token summaries has headroom, but the longest summaries in
-- prod run 79 words (~105 tokens), and five of those land near 575 plus JSON
-- scaffolding. A response cut off at the cap is truncated JSON, which fails
-- alignment exactly the way a scrambled response does — except the fix would
-- be a bigger ceiling, not a re-ask.
--
-- ANSWERED 2026-08-11, AND BOTH HALVES WERE WRONG. Over 212 calls:
--
--     0 ended in max_tokens (every one end_turn); peak output 481 of 700
--     1 misaligned call, 0.5% — not the 4-12% this was premised on
--
-- The 4-12% was a measurement artifact. It came from inferring re-asks as the
-- excess of calls over ceil(articles / BATCH_SIZE) in ai_usage_daily, which
-- counts every partial last-batch as a retry: 18 of the 212 calls ran below
-- BATCH_SIZE, carrying 43 articles that would pack into 9 calls if filled.
-- That is ~9 of the 9 "excess" calls. Packing across runs would mean holding
-- articles back from a pipeline that runs every 30 minutes, so it is not
-- recoverable waste either.
--
-- The table stays, for a purpose it was not built for: the aligned/misaligned
-- split is the only stored signal for whether a model returns parseable
-- indexed JSON at all. That is what caught gpt-5-nano producing 30/30 empty
-- batches at this same max_tokens — spending its whole budget reasoning, with
-- zero provider errors — which would otherwise have degraded to truncated RSS
-- text while the run reported success.
--
-- The answer had to be queryable rather than logged, for the same reason as
-- 018_threading_shadow: the Railway log buffer rotates and resets on deploy,
-- so a 24h aggregate cannot be reconstructed from it.
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
