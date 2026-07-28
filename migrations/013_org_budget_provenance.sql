-- Migration 013: budget figures get a fiscal year and a source; drop political_lean
--
-- WHY (sift/STATUS.md, 2026-07-28):
--   All ten think-tank rows in data/org_profiles.csv are demo fixtures. The
--   commit that created that file (#26, 100789a) calls them "example rows ...
--   during local development" and "starter rows to exercise the seed pipeline".
--   The politician CSV was replaced with real data in Phase 3.B; the org CSV
--   never was, so the fixtures shipped to production and stayed.
--
--   Every one of the ten budget figures was wrong — all 1-2 significant
--   figures, all round millions, with $9M and $55M each appearing twice.
--   Errors ran both directions (Heritage understated ~$44M, Manhattan
--   overstated ~3x). Five of ten ProPublica links returned 404.
--
-- WHAT CHANGES:
--   annual_budget_usd now means TOTAL FUNCTIONAL EXPENSES from a specific
--   Form 990, and cannot render without the fiscal year and source URL that
--   make it checkable. "Annual budget" was never a checkable claim; "total
--   expenses, FY Dec 2024, per this filing" is.
--
--   Expenses rather than revenue because it matches what organizations mean by
--   "operating budget" — EPI's own site states a 2024 operating budget of
--   $13.3M against $13,581,055 in total functional expenses and $11.9M revenue.
--
-- POLITICAL_LEAN IS DROPPED HERE, not merely deprecated. Migration 012 left it
--   in place for rollback and stopped rendering it on the dossier — but /civic
--   kept publishing it for all 103 orgs for another day, because a column that
--   still exists is a column something can still read. Same failure shape as
--   the "Edited by Claude" masthead: removed from the plan, live in the product.

ALTER TABLE org_profiles
  ADD COLUMN IF NOT EXISTS annual_budget_fy     TEXT,  -- e.g. 'FY ending December 2024'
  ADD COLUMN IF NOT EXISTS annual_budget_source TEXT;  -- the specific 990 / filing page

COMMENT ON COLUMN org_profiles.annual_budget_usd IS
  'Total functional expenses from the Form 990 identified by annual_budget_source. NOT a general "annual budget" — that was the unsourced fixture value this replaced. Requires annual_budget_fy + annual_budget_source to render.';
COMMENT ON COLUMN org_profiles.annual_budget_fy IS
  'Fiscal year the figure covers, as stated by the filing. Required alongside annual_budget_usd.';

ALTER TABLE org_profiles DROP COLUMN IF EXISTS political_lean;
