-- Shared admission policy: background cannot consume Compare's protected share.
CREATE TABLE IF NOT EXISTS ai_budget_policy (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
 enabled BOOLEAN NOT NULL, daily_limit_usd DOUBLE PRECISION NOT NULL,
 compare_share DOUBLE PRECISION NOT NULL CHECK (compare_share BETWEEN 0 AND 1)
);
CREATE TABLE IF NOT EXISTS ai_budget_holds (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK (scope IN ('background', 'compare')),
 remaining_usd DOUBLE PRECISION NOT NULL CHECK (remaining_usd >= 0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), settled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ai_budget_holds_unsettled ON ai_budget_holds(created_at) WHERE settled_at IS NULL;
ALTER TABLE api_batches ADD COLUMN IF NOT EXISTS budget_hold_id TEXT;
CREATE OR REPLACE FUNCTION sift_reserve_ai_budget(hold_id TEXT, amount DOUBLE PRECISION, budget_scope TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE policy ai_budget_policy%ROWTYPE; committed DOUBLE PRECISION; ceiling DOUBLE PRECISION;
BEGIN
 PERFORM pg_advisory_xact_lock(73482691);
 SELECT * INTO STRICT policy FROM ai_budget_policy WHERE singleton;
 IF amount <= 0 OR amount = 'Infinity'::float8 OR amount = 'NaN'::float8
    OR budget_scope NOT IN ('background', 'compare') THEN RETURN FALSE; END IF;
 SELECT COALESCE(SUM(estimated_cost_usd), 0) INTO committed FROM ai_usage_daily
 WHERE usage_date = (NOW() AT TIME ZONE 'UTC')::date;
 SELECT committed + COALESCE(SUM(remaining_usd), 0) INTO committed FROM ai_budget_holds WHERE settled_at IS NULL;
 ceiling := policy.daily_limit_usd;
 IF budget_scope = 'background' THEN ceiling := ceiling * (1 - policy.compare_share); END IF;
 IF policy.enabled AND policy.daily_limit_usd > 0 AND committed + amount > ceiling THEN RETURN FALSE; END IF;
 INSERT INTO ai_budget_holds(id, scope, remaining_usd) VALUES (hold_id, budget_scope, amount);
 RETURN TRUE;
END $$;
