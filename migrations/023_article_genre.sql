-- Opinion-genre flag (ranking v2 stage 4, sift/docs/RANKING_SIGNALS.md).
-- Set at store time by services/genre.py from outlet-declared signals only
-- (URL path segments like /opinion/, /commentisfree/; title prefixes like
-- "Opinion:"). Evidence: the first hand-labeled ranking eval (#200) — half
-- the labeler's overrules rejected op-eds the formulas had ranked as news.
--
-- DEFAULT FALSE, not nullable: absence of an opinion marker IS the reported
-- verdict under a precision-first heuristic; there is no unknown state to
-- represent (unlike tone, where NULL = not-yet-classified-by-the-model).
--
-- Consumed by the read path (sift/lib/db.ts + NewsAggregator.tsx): opinion
-- articles rank x0.6 and opinion-backed framings are excluded from the
-- cross-spectrum corroboration bonus.
--
-- Also extends feed_balance (migrations/022) with the recorded-but-untripped
-- opinion share of the ranked top 10.
--
-- Same DDL applied at startup by app/db.py:_apply_migrations (the prod
-- apply path on Railway); this file is documentation + manual ops.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS is_opinion BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE feed_balance ADD COLUMN IF NOT EXISTS opinion_share_top10 REAL;
