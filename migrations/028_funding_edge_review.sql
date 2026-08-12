-- Human adjudication for funding edges the automated check held back.
--
-- Migration 027 gave every edge an `ein_name_agrees` verdict and made only
-- 'agrees' publishable. That was the right gate and an incomplete feature:
-- a held edge had no way to become unheld. Harvard Law School filed under
-- President and Fellows of Harvard College is legitimate and would have sat
-- withheld forever, because no string comparison can confirm it and nothing
-- let a person say so.
--
-- The decision is a SEPARATE LAYER, not an overwrite. `ein_name_agrees` keeps
-- recording what the machine found; these columns record what a person
-- concluded and why. Collapsing them into one field would destroy the more
-- interesting fact — that the check fired and a human disagreed — and would
-- make a later re-ingest silently look like it had always agreed.
--
--   review_decision  'confirmed' -> the filed name really is this EIN; publish
--                    'rejected'  -> genuinely wrong; never publish
--                    NULL        -> nobody has looked yet
--
-- A rejected edge is kept rather than deleted: the fact that a filer misfiled
-- a grant is itself a finding, and deleting it invites a future re-ingest to
-- silently resurrect it.
--
-- Applied in prod by app/db.py:_apply_migrations at startup; this file is
-- documentation + manual ops.

ALTER TABLE funding_edges
    ADD COLUMN IF NOT EXISTS review_decision TEXT
        CHECK (review_decision IN ('confirmed', 'rejected'));

ALTER TABLE funding_edges
    ADD COLUMN IF NOT EXISTS review_note TEXT;

ALTER TABLE funding_edges
    ADD COLUMN IF NOT EXISTS reviewed_by TEXT;

ALTER TABLE funding_edges
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;

-- The read path asks for "publishable", which is now the machine verdict OR a
-- human confirmation, minus anything explicitly rejected.
CREATE INDEX IF NOT EXISTS idx_funding_edges_decision
    ON funding_edges (review_decision)
    WHERE review_decision IS NOT NULL;
