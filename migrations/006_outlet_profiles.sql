-- Outlet provenance for the civic-literacy MVP (Phase 2.A).
--
-- See plans/sift-phase-2-cross-spectrum-and-outlet-provenance.md.
--
-- outlet_profiles holds the curated metadata Sift exposes on each
-- outlet's dossier page: ownership, funding, AllSides political-lean
-- rating, MBFC factual-accuracy rating, links out to those sources.
-- Sift never asserts a rating itself — it surfaces AllSides' / MBFC's
-- ratings with citation. Symmetric application across the political
-- spectrum is the methodology, documented at /methodology.
--
-- source_name_aliases maps the messy free-text source_name values that
-- come out of RSS (e.g., "Reuters", "Reuters.com", "Reuters | Breaking
-- news worldwide") onto a single canonical outlet_slug. Articles whose
-- source_name doesn't appear in this table render without an outlet
-- provenance affordance — graceful degradation.
--
-- Both tables are populated via:
--   - data/outlet_profiles.csv  (hand-curated; quarterly review)
--   - scripts/seed_outlet_profiles.py
--   - scripts/audit_source_aliases.py  (suggests new aliases from
--     unmatched articles.source_name values)
--   - scripts/seed_source_aliases.py

CREATE TABLE IF NOT EXISTS outlet_profiles (
  slug                  TEXT PRIMARY KEY,
  name                  TEXT NOT NULL,
  parent_company        TEXT,
  parent_company_url    TEXT,
  founded_year          INT,
  funding_model         TEXT,        -- 'subscription' | 'advertising' | 'foundation' | 'donations' | 'mixed' | 'public-service'
  major_funders         JSONB DEFAULT '[]'::jsonb,
  allsides_rating       TEXT,        -- 'left' | 'lean-left' | 'center' | 'lean-right' | 'right' | 'mixed'
  allsides_url          TEXT,
  allsides_last_checked DATE,
  mbfc_factual          TEXT,        -- 'high' | 'mostly-factual' | 'mixed' | 'low' | 'very-low'
  mbfc_url              TEXT,
  mbfc_last_checked     DATE,
  notes                 TEXT,
  external_links        JSONB DEFAULT '{}'::jsonb,
  updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outlet_profiles_name_lower
  ON outlet_profiles (LOWER(name));

CREATE TABLE IF NOT EXISTS source_name_aliases (
  raw_source_name TEXT PRIMARY KEY,
  outlet_slug     TEXT NOT NULL REFERENCES outlet_profiles (slug) ON DELETE CASCADE,
  added_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_name_aliases_slug
  ON source_name_aliases (outlet_slug);
