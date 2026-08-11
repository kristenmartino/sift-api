-- Article genre (ranking v2 stage 6): news | feature | soft.
-- Produced by the same Haiku call as why_it_matters, importance_score and
-- tone (services/context_generator.py) — a fourth key, not a new call.
--
-- is_opinion (023) and is_roundup (024) catch what outlets DECLARE via URL
-- paths and show titles. This catches what they don't: magazine-style
-- features and soft/curiosity pieces that read as news and can score
-- importance 3+. It is deliberately NOT a spectacle detector — crime
-- spectacle is already correctly scored importance 1-2, and the
-- low-importance weight in sift/lib/db.ts handles that population.
--
-- NULL = not yet classified; every ranking site treats NULL as "news"
-- (fail-open — a missing signal never penalizes an article).
-- Consumed by the read path for STANDALONE articles only: story ranking is
-- corroboration-based and unaffected, so a tabloid or feature piece inside
-- a multi-outlet story still appears under "how this was covered".
--
-- Same DDL applied at startup by app/db.py:_apply_migrations.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS genre TEXT
    CHECK (genre IN ('news', 'feature', 'soft'));
