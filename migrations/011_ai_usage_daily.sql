-- 011_ai_usage_daily.sql
-- Daily AI cost ledger backing the cost ceiling (sift-api#70).
--
-- One row per (UTC date, provider, model, operation). services/cost_guard.py
-- sums the day's estimated_cost_usd to (a) hard-stop live paid calls once the
-- daily limit is reached and (b) alert at 80% of budget. Covers the live paid
-- paths only — compare web-search (Claude) + Voyage embeddings. Frontend
-- topic-search paid calls remain a temporary D35 exception until sift-api#79
-- moves that fallback into sift-api.
--
-- Applied at startup (non-CONCURRENTLY, idempotent) by
-- app/db.py:_apply_migrations. This file is the manual-ops / documentation copy.

CREATE TABLE IF NOT EXISTS ai_usage_daily (
    usage_date          DATE NOT NULL,
    provider            TEXT NOT NULL,          -- 'anthropic' | 'voyage'
    model               TEXT NOT NULL,
    operation           TEXT NOT NULL,          -- call-site id, e.g. 'compare.search'
    estimated_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    call_count          INTEGER NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, provider, model, operation)
);
