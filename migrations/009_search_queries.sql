-- Phase 1 search-funnel instrumentation.
--
-- One row per search request. The Next.js topic-search route writes
-- here at end-of-stream (fire-and-forget). Goal: enough signal in
-- 7-14 days to decide whether the next search investment goes into
-- (a) entity-aware resolution (Phase 2) or (b) HNSW + re-ranking
-- (Phase 3). See sift PR for the matching INSERT logic.
--
-- Privacy posture:
--   - Raw IPs are NEVER persisted. `ip_hash` is HMAC-SHA256(ip, SECRET).
--   - Query text IS stored verbatim — necessary to find top queries
--     and to build a real eval set. Retention is capped at 90 days
--     via scripts/cleanup_old_search_queries.py (run periodically).
--   - Session id is a localStorage UUID set client-side; not tied to
--     Clerk auth, not a tracking cookie.
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction
-- block. The app-startup migration path in app/db.py applies the same
-- DDL without CONCURRENTLY (idempotent via IF NOT EXISTS); operators
-- applying this file manually should comment out CONCURRENTLY or use
-- psql with one-statement-per-call.
--
-- pgcrypto provides gen_random_uuid() for the row id default — most
-- Neon projects already have it; CREATE EXTENSION IF NOT EXISTS is a
-- no-op when present.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS search_queries (
  id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  query                   TEXT NOT NULL,                      -- raw, max 200 chars per route guard
  query_norm              TEXT NOT NULL,                      -- lowercased, whitespace-collapsed
  query_token_count       INT NOT NULL,                       -- proxy for "name" vs "question"
  result_count_vector     INT NOT NULL,                       -- passed SIMILARITY_THRESHOLD
  result_count_total      INT NOT NULL,                       -- after web-fallback dedup
  fallback_used           BOOLEAN NOT NULL DEFAULT FALSE,
  latency_ms_total        INT NOT NULL,
  latency_ms_embed        INT,
  latency_ms_vector       INT,
  latency_ms_fallback     INT,                                -- null when fallback not used
  session_id              TEXT,
  ip_hash                 TEXT,                               -- HMAC-SHA256, never raw
  user_agent_class        TEXT,                               -- mobile|desktop|bot|unknown
  -- Phase 2 hooks — null today; populated when entity-aware search ships.
  matched_entity_type     TEXT,                               -- politician|org|bill|outlet
  matched_entity_id       TEXT
);

-- Time-ordered scan for "queries in the last N days" aggregations.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_search_queries_created
  ON search_queries(created_at DESC);

-- Top-query rollups (GROUP BY query_norm).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_search_queries_query_norm
  ON search_queries(query_norm);
