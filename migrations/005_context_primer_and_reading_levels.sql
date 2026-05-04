-- Context primer + reading levels for the civic-literacy MVP (Phase 1).
--
-- See plans/sift-civic-literacy.md for full context. Both columns are
-- additive and nullable; existing rows get NULL until backfill (Phase 1A
-- only ships primer; reading_levels arrives in Phase 1B).
--
-- context_primer  — "What you should know first" panel:
--   { background: string,
--     terms: [{ term: string, definition: string, source?: string }],
--     generated_at: ISO8601 string }
--
-- reading_levels  — Claude rewrites of the article body for two non-default
-- reading levels (simpler + detailed). Only populated for long-form articles
-- (>~800 words); short wires render the standard summary at all levels.
--   { simpler:  { headline: string, summary: string },
--     detailed: { headline: string, summary: string },
--     generated_at: ISO8601 string }
--
-- These are read by sift/lib/db.ts and rendered by the primer + reading-level
-- slider components in sift/components/primer/.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS context_primer JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS reading_levels JSONB;
