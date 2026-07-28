-- Migration 012: Replace Sift-assigned org political_lean with cited self-description
--
-- WHY (LAUNCH_DECISION_MEMO.md §2.2e, §5 B6):
--   `org_profiles.political_lean` was hand-authored in data/org_profiles.csv and
--   rendered at 26px on /org/[slug] as a bare assertion — no source, no date, no
--   method. scripts/seed_org_profiles.py only enum-validates it; nothing derives
--   or cites it. That is Sift computing its own political rating, which D37
--   explicitly forbids ("Sift surfaces ratings verbatim and never computes its
--   own"), and it did so with more visual weight than any cited claim on the
--   page.
--
--   Worse, the same column name means two different things across surfaces:
--   on /outlet/[slug] `political_lean` is AllSides' rating, rendered WITH
--   "Source: AllSides · last verified <date>". On /org/[slug] it was Sift's
--   judgment, rendered without. A reader could not tell them apart.
--
-- WHAT REPLACES IT:
--   The organization's own self-description, quoted verbatim and linked to the
--   page it came from. Same posture as AllSides on outlets — surface, don't
--   compute — but the primary record here is the org's own words, which is
--   checkable in one click and carries more information than a seven-value
--   bucket ("libertarian" and "conservative" both flattened to `lean-right`).
--
--   A self-description is what an organization says about itself, not an
--   independent assessment. The UI must label it that way.
--
-- AGENCIES: `political_lean` was 'nonpartisan' on all 15 federal agencies,
--   which is a category error — an independent commission's structure is not
--   the same claim as a think tank's self-description. Agencies get
--   `governance_structure` instead: statutory facts that do not rot. Deliberately
--   NOT stored: current chair, current composition, appointing president. Those
--   change with every administration and this repo has no refresh job — the same
--   failure that left interest_group_ratings empty on 536 rows and PAC data
--   pinned to the 2022 cycle.

ALTER TABLE org_profiles
  ADD COLUMN IF NOT EXISTS self_description        TEXT,  -- verbatim, the org's own words
  ADD COLUMN IF NOT EXISTS self_description_source TEXT,  -- URL the quote came from
  ADD COLUMN IF NOT EXISTS self_description_checked DATE, -- when a human last verified the quote
  ADD COLUMN IF NOT EXISTS governance_structure    TEXT,  -- agencies only; statutory, stable
  ADD COLUMN IF NOT EXISTS governance_source       TEXT;  -- URL (US Code, agency site)

COMMENT ON COLUMN org_profiles.self_description IS
  'The organization''s own characterization of itself, quoted verbatim. Never Sift''s assessment. Requires self_description_source to render.';
COMMENT ON COLUMN org_profiles.governance_structure IS
  'Statutory governance facts for agencies (branch, appointment, partisan-balance cap). Stable by design — no current-composition data.';
COMMENT ON COLUMN org_profiles.political_lean IS
  'DEPRECATED 2026-07-27 (migration 012). Sift-assigned, uncited, contrary to D37. Retained for rollback only; not rendered. Drop once 012 is verified in prod.';
