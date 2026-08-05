-- Migration 015: structured, primary-record role provenance for executive rows
--
-- WHY:
--   All 102 politician_profiles rows with chamber IN ('executive',
--   'foreign-executive') shipped with a populated freeform `notes` field
--   carrying uncited biographical claims and characterizations about living
--   people:
--
--     EXEC-AUSTIN-L   "U.S. Secretary of Defense (2021-2025) under Biden.
--                      First African-American Secretary of Defense."
--     EXEC-BESSENT-S  "U.S. Secretary of the Treasury (2025-). Former
--                      hedge-fund executive (Key Square Group)."
--
--   This is the same defect sift/STATUS.md:103 caught in org_profiles.notes
--   ("Uncited prose; several are characterizations") and migration 013
--   removed -- reproduced in a population created afterwards. It is sharper
--   here: sift/docs/OPERATING_CONTEXT.md §5 forbids a dossier claim about a
--   living person without a citation to the primary record.
--
--   The Wikipedia link in external_links does not rescue it. STATUS.md:109-113
--   already rejected Wikipedia as a source when it dropped org founded_year
--   rather than cite it -- "one unsourced field on an otherwise fully-sourced
--   page is the field a reader spot-checks precisely because everything else
--   is cited."
--
--   Only /outlet/* renders an article list, so politician pages are
--   profile-only: on these 102 rows the uncited notes ARE the page. They are
--   consequently excluded from sift/app/sitemap.ts via listSitemapEntries()
--   in sift/lib/db.ts -- which withholds Trump, Biden, Putin and the entire
--   Cabinet, among the highest-mention entities in the corpus.
--
-- WHAT CHANGES:
--   Freeform prose is replaced by fields that each name their own primary
--   record. Every claim-bearing column is paired with a source column, on the
--   013 pattern (annual_budget_usd / _fy / _source), so a value physically
--   cannot render without the record that backs it:
--
--     role_title             <- role_title_source      (statute establishing the office)
--     role_start/end_date    <- role_dates_source
--     nomination_date        <- nomination_url         (congress.gov PN record)
--     predecessor_name       <- predecessor_source     (a PN "vice" clause, or
--                                                     the prior roll-call)
--     confirmation_date      <- confirmation_vote_url  (senate.gov roll-call)
--     confirmation_vote_result <- confirmation_vote_url
--
--   The sources are machine-gathered, not recalled: scripts/
--   scrape_executive_records.py reads senate.gov roll-call vote menus and the
--   api.congress.gov nominations endpoint. Recall is what produced the
--   org fixtures (013) and the Brookings FARA claim (STATUS.md:80-84).
--
--   `notes` is NOT dropped as a column -- 536 sitting-Congress rows use it
--   legitimately. It is cleared on the 102 executive rows by
--   scripts/seed_executive_records.py, in the same transaction that writes
--   their replacements.
--
-- PREDECESSOR, AND WHY IT IS SOURCEABLE AT ALL:
--   A Senate nomination record states its own predecessor verbatim -- PN650
--   reads "...to be Administrator of the National Aeronautics and Space
--   Administration, vice Bill Nelson, resigned." That clause is a primary
--   record. A predecessor list read off Wikipedia would not be, and is
--   exactly the substitution 013 refused.
--
--   That clause is absent from the en-bloc nominations an incoming
--   administration files -- PN11-1 reads only "Scott Bessent, of South
--   Carolina, to be Secretary of the Treasury." because the office falls
--   vacant at the transition rather than "vice" a named person. It covers 2
--   of 37 rows on its own.
--
--   The remaining 35 come from the Senate's own record instead: the previous
--   confirmation to the same office. predecessor_source records WHICH of the
--   two it was, because they are different claims. The nomination clause is
--   "the record names this person"; a prior roll-call is "the Senate last
--   confirmed this person to this office", which is narrower -- it is silent
--   about acting officials, who are never confirmed. The UI must say the
--   narrower thing, so the column that distinguishes them is not optional.
--
-- ID_SOURCE:
--   Phase 4 keeps executive officials in politician_profiles under synthetic
--   primary keys ('EXEC-TRUMP-DJ') rather than adding an official_profiles
--   table, which would need a new entity_links.type value -- a breaking change
--   across sift/lib/entityLinks.ts, types.ts, EntityChipTooltip.tsx,
--   CivicIndex.tsx and entity_linker_llm.py's roster headings. Renaming the
--   bioguide_id column is likewise not worth the bill_profiles.sponsor_bioguide
--   FK, three indexes, the [bioguide] route param, and every canonical_id
--   already stored in articles.entity_links.
--
--   id_source makes that compromise legible in the schema instead of leaving
--   it as folklore, the way 012/013 made the org compromises legible.

ALTER TABLE politician_profiles
  ADD COLUMN IF NOT EXISTS id_source                TEXT,
  ADD COLUMN IF NOT EXISTS role_title               TEXT,
  ADD COLUMN IF NOT EXISTS role_title_source        TEXT,
  ADD COLUMN IF NOT EXISTS role_start_date          DATE,
  ADD COLUMN IF NOT EXISTS role_end_date            DATE,
  ADD COLUMN IF NOT EXISTS role_dates_source        TEXT,
  ADD COLUMN IF NOT EXISTS nomination_date          DATE,
  ADD COLUMN IF NOT EXISTS nomination_url           TEXT,
  ADD COLUMN IF NOT EXISTS confirmation_date        DATE,
  ADD COLUMN IF NOT EXISTS confirmation_vote_url    TEXT,
  ADD COLUMN IF NOT EXISTS confirmation_vote_result TEXT,
  ADD COLUMN IF NOT EXISTS predecessor_name         TEXT,
  ADD COLUMN IF NOT EXISTS predecessor_source       TEXT;

COMMENT ON COLUMN politician_profiles.bioguide_id IS
  'Congress.gov bioguide ID when id_source = ''bioguide''. Otherwise a synthetic Sift identifier (EXEC-TRUMP-DJ, FOREIGN-PUTIN-V) -- see id_source. Not renamed because bill_profiles.sponsor_bioguide, the /politician/[bioguide] route param, and every canonical_id already written into articles.entity_links depend on it.';
COMMENT ON COLUMN politician_profiles.id_source IS
  'Provenance of bioguide_id: ''bioguide'' | ''executive'' | ''foreign-executive'' | ''scotus''. Any value other than ''bioguide'' means the PK is a synthetic Sift id.';
COMMENT ON COLUMN politician_profiles.role_title IS
  'Office title exactly as the establishing record states it ("Secretary of Defense", not "Defense Secretary"). Requires role_title_source to render.';
COMMENT ON COLUMN politician_profiles.role_title_source IS
  'URL of the record establishing the office -- a U.S. Code section on uscode.house.gov, a constitutional provision on constitution.congress.gov, or the office''s own official site for non-statutory and foreign posts.';
COMMENT ON COLUMN politician_profiles.role_dates_source IS
  'Primary record for role_start_date / role_end_date. For Senate-confirmed officials this is usually the same roll-call as confirmation_vote_url; for elected and appointed posts it is the record of election or appointment.';
COMMENT ON COLUMN politician_profiles.nomination_url IS
  'congress.gov PN record. Sources nomination_date.';
COMMENT ON COLUMN politician_profiles.confirmation_vote_url IS
  'senate.gov roll-call vote page. Sources BOTH confirmation_date and confirmation_vote_result.';
COMMENT ON COLUMN politician_profiles.confirmation_vote_result IS
  'Verbatim Senate outcome and tally, e.g. "Confirmed 50-50". Sift never recomputes or characterizes it.';
COMMENT ON COLUMN politician_profiles.predecessor_name IS
  'Previous holder of this office. NULL unless predecessor_source is set.';
COMMENT ON COLUMN politician_profiles.predecessor_source IS
  'The record behind predecessor_name, and which of two claims it supports. A congress.gov PN URL means the nomination''s verbatim "vice <name>" clause named them. A senate.gov roll-call URL means that is the Senate''s previous confirmation to this office -- narrower, and silent about acting officials, who are never confirmed.';

-- Publish gate (sift/lib/db.ts listSitemapEntries) reads role_title +
-- role_title_source on these rows; every executive lookup filters by chamber
-- first, so the partial index matches the query shape.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_politician_profiles_sourced_role
  ON politician_profiles (chamber)
  WHERE role_title IS NOT NULL AND role_title_source IS NOT NULL;
