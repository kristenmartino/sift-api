-- Phase 1 primer-expand instrumentation.
--
-- After three rounds of work on the "What you should know first" panel
-- (prompt tightening in PR #48, primer-term dossier links in #90,
-- agency dossiers + linker upgrades in #91/#46) we still have zero
-- signal on whether ANYONE opens the panel. The two-options choice
-- between "keep iterating on primer content" vs "stop and move on"
-- depends on that one data point.
--
-- This table records every panel-expand click. Deliberately NOT tracking
-- impressions — that would generate ~5k writes/day on data we can
-- already compute (which articles in the feed have primers from
-- articles.context_primer IS NOT NULL).
--
-- Privacy posture mirrors search_queries (migrations/009):
--   - Raw IPs are NEVER persisted. `ip_hash` is HMAC-SHA256(ip, SECRET).
--   - 90-day retention via scripts/cleanup_old_primer_events.py.
--   - session_id is a localStorage UUID, not a cookie.
--   - Reuses SEARCH_IP_SECRET (treat it as a general analytics secret).
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction
-- block. The app-startup migration path in app/db.py applies the same
-- DDL without CONCURRENTLY (idempotent via IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS primer_expand_events (
  id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  article_id              TEXT,                        -- nullable: future surfaces may not have one
  surface                 TEXT,                        -- 'feed' | 'bookmarks' | future
  session_id              TEXT,
  ip_hash                 TEXT,                        -- HMAC-SHA256, never raw
  user_agent_class        TEXT                         -- mobile|desktop|bot|unknown
);

-- Time-ordered scan for "last N days" rollups.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_primer_expand_events_created
  ON primer_expand_events(created_at DESC);

-- Per-article expand counts (which articles' primers get opened most).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_primer_expand_events_article
  ON primer_expand_events(article_id)
  WHERE article_id IS NOT NULL;
