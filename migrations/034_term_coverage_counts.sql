-- 034_term_coverage_counts.sql
--
-- Denormalised coverage counts on term_profiles, so /glossary and the sitemap
-- stop recomputing them across the corpus on every read.
--
-- Why
-- ---
-- "Which articles involve this term" is a lateral over term x surface-form x
-- corpus. Measured against prod as the table grew:
--
--     24 terms    785 ms   (33 ms/term)
--     37 terms  1,522 ms   (41 ms/term)
--
-- The slope steepens because more terms means more matched rows to aggregate,
-- so this is worse than linear. At ~100 terms it is several seconds, which is
-- where a serverless regeneration starts failing rather than merely feeling
-- slow. Only two surfaces scale this way -- /glossary and the sitemap floor.
-- `/term/<slug>` is per-term and stays at 34-81 ms however many terms exist,
-- so it keeps computing live and is unaffected by any of this.
--
-- Deliberately NOT a (term, article) join table
-- ---------------------------------------------
-- That is the general shape, and nothing needs it. It would also require a
-- hook in the ingest pipeline next to link_entities -- the write path's hot
-- loop, and where D54's invisible-cost mistakes live. Three columns and a
-- script on the seeders' cadence buy the same read performance with no
-- pipeline coupling and a one-line revert. If a per-article question ever
-- turns up ("articles mentioning both certiorari and qualified immunity")
-- the join table is still available, and these counts become derived from it.
--
-- The honesty cost, and how it is paid
-- ------------------------------------
-- The read-time query is always right. A stored count is right as of when it
-- ran. That is a real property to give up on a product whose whole claim is
-- not asserting numbers it cannot stand behind, so:
--
--   * coverage_computed_at is stored alongside, and /glossary renders "as of
--     <date>" rather than implying now.
--   * A NULL stamp means never computed, and the publish floor treats that as
--     zero coverage -- so a freshly seeded term is withheld until it has been
--     measured, rather than published on a guess. Fails closed.
--   * `/term/<slug>` still computes live, so the authoritative per-term view
--     is never stale. The index is explicitly a periodic summary.
--
-- The floor reads these columns EVERYWHERE, including on the term page's own
-- generateMetadata. That is the point: sitemap membership and a page's robots
-- tag have to come from one source, or a page says noindex while the sitemap
-- advertises it.

ALTER TABLE term_profiles
  ADD COLUMN IF NOT EXISTS article_count        INTEGER,
  ADD COLUMN IF NOT EXISTS outlet_count         INTEGER,
  -- Articles matched only via the primer, i.e. the coverage never prints the
  -- term. /glossary's headline finding is built on this.
  ADD COLUMN IF NOT EXISTS unnamed_count        INTEGER,
  ADD COLUMN IF NOT EXISTS coverage_computed_at TIMESTAMPTZ;

COMMENT ON COLUMN term_profiles.coverage_computed_at IS
  'When scripts/refresh_term_coverage.py last measured the three count '
  'columns. NULL means never measured, which the publish floor treats as '
  'zero coverage rather than as unknown.';
