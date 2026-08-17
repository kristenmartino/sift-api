-- 031_term_profiles.sql
--
-- Curated definitions for the civic vocabulary that shows up in the news.
--
-- Why a table and not the primers we already have
-- ------------------------------------------------
-- `articles.context_primer` already defines ~11,900 distinct terms, and those
-- definitions are good. They are also **unsourced** — every `terms[].source`
-- in the corpus is null, because primer_generator was never asked for one.
-- Publishing them as standalone pages would state a definition of a legal
-- term on Sift's own authority, which is the defect migration 013 removed
-- from org_profiles and 015 removed from the executive rows. Third time.
--
-- So a term reaches `/term/<slug>` only when a human has written a definition
-- and attached the authority it came from. The primers stay where they are:
-- inline reading aid, never a published claim.
--
--   definition         Sift's plain-language rendering. NOT a quotation —
--                      the UI labels it as a summary, not the source's words.
--   definition_source  The authority it was drawn from. Required to publish;
--                      lib/term.ts drops the pair without it, the same way
--                      lib/org.ts nulls the budget triple.
--   definition_checked When a human last read the source and confirmed the
--                      summary still matches it. Precedent:
--                      org_profiles.self_description_checked.
--
-- The other half of the page — which articles mention the term, from which
-- outlets, with which published ratings — is computed from the corpus at read
-- time and stores nothing. That half is reportage about our own index, not a
-- claim, so it needs no citation beyond the articles it links.

CREATE TABLE IF NOT EXISTS term_profiles (
  slug               TEXT PRIMARY KEY,
  term               TEXT NOT NULL,
  definition         TEXT NOT NULL,
  definition_source  TEXT NOT NULL,
  definition_checked DATE,
  -- Surface forms to match in article text beyond `term` itself, e.g.
  -- {"TPS"} for temporary protected status. Matched whole-word by the read
  -- query; kept deliberately small and hand-checked, per the #40 rule that
  -- derived aliases are what go wrong.
  aliases            JSONB NOT NULL DEFAULT '[]'::jsonb,
  category           TEXT,
  notes              TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_term_profiles_term_lower
  ON term_profiles (LOWER(term));

COMMENT ON COLUMN term_profiles.definition IS
  'Sift''s plain-language summary of the term, drawn from definition_source. '
  'Not a quotation. Renders only with its source — an uncited definition of a '
  'legal term is a claim on Sift''s own authority.';
