-- Politician + org + bill curated profile tables for the civic-literacy
-- MVP (Phase 3.A).
--
-- See plans/sift-civic-literacy.md (Phase 3) for context. The "inline
-- glossary" feature surfaces verifiable, externally-cited context for
-- politicians, advocacy orgs / think tanks, and bills mentioned in
-- articles. Same editorial discipline as outlet_profiles: every panel
-- links to its underlying source (GovTrack, OpenSecrets, ProPublica
-- Nonprofit Explorer, FARA, Vote Smart). Sift never asserts a number;
-- it surfaces public-record numbers with attribution.
--
-- Phase 3.A creates the schema only. Population happens in:
--   - Phase 3.B  GovTrack scrape → politician_profiles (all 535
--                sitting Congress members)
--   - Phase 3.D  hand-curate ~200 think tanks / advocacy orgs in
--                data/org_profiles.csv
--   - Phase 3.E  OpenSecrets daily refresh enriches politician rows
--                with top_industries_current_cycle + interest_group_ratings
--   - Phase 3.F  bill_profiles populated on-demand when articles
--                reference a bill not yet in the table
--
-- All three tables follow outlet_profiles' pattern:
--   - Primary key is a stable canonical identifier
--   - JSONB columns for list/object data (with sane defaults)
--   - external_links is a free-form bag of citation URLs
--   - notes is reviewer free-form prose
--   - updated_at / refreshed_at separate human-edit time from
--     automated-refresh time

CREATE TABLE IF NOT EXISTS politician_profiles (
  bioguide_id                  TEXT PRIMARY KEY,                 -- Congress.gov canonical ID, e.g. 'S001181' (Schumer)
  name                         TEXT NOT NULL,
  party                        TEXT,                             -- 'D' | 'R' | 'I' | other
  state                        TEXT,                             -- USPS code, e.g. 'NY'
  chamber                      TEXT,                             -- 'senate' | 'house' | 'former' | 'executive'
  committees                   JSONB DEFAULT '[]'::jsonb,        -- ["Banking", "Energy & Natural Resources", ...]
  top_industries_current_cycle JSONB DEFAULT '[]'::jsonb,        -- [{industry: "Securities", amount_usd: 1200000}, ...]
  interest_group_ratings       JSONB DEFAULT '{}'::jsonb,        -- { LCV: 92, NRA: "F", ADA: 88, ACU: 6, ... }
  external_links               JSONB DEFAULT '{}'::jsonb,        -- { govtrack, opensecrets, votesmart, ballotpedia, wikipedia }
  notes                        TEXT,
  refreshed_at                 TIMESTAMPTZ DEFAULT NOW(),        -- last automated-refresh time
  updated_at                   TIMESTAMPTZ DEFAULT NOW()         -- last human-edit time
);

CREATE INDEX IF NOT EXISTS idx_politician_profiles_name_lower
  ON politician_profiles (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_politician_profiles_state_party
  ON politician_profiles (state, party);
CREATE INDEX IF NOT EXISTS idx_politician_profiles_chamber
  ON politician_profiles (chamber);

CREATE TABLE IF NOT EXISTS org_profiles (
  slug              TEXT PRIMARY KEY,                            -- e.g. 'brookings-institution', 'heritage-foundation'
  name              TEXT NOT NULL,
  type              TEXT,                                        -- 'think-tank' | 'advocacy' | 'union' | 'pac' | 'super-pac' | 'foundation' | 'industry-group' | 'other'
  political_lean    TEXT,                                        -- 'left' | 'lean-left' | 'center' | 'lean-right' | 'right' | 'mixed' | 'nonpartisan'
  founded_year      INT,
  annual_budget_usd NUMERIC,                                     -- approx; from latest 990 or self-reported
  major_funders     JSONB DEFAULT '[]'::jsonb,                   -- ["Hutchins Family", "Rockefeller Foundation", ...]
  fara_registered   BOOLEAN DEFAULT FALSE,                       -- TRUE if any FARA filings exist for this org or its principals
  fara_countries    JSONB DEFAULT '[]'::jsonb,                   -- ["Qatar", "Saudi Arabia", ...]
  external_links    JSONB DEFAULT '{}'::jsonb,                   -- { propublica, irs_990, fara, wikipedia, official }
  notes             TEXT,
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_profiles_name_lower
  ON org_profiles (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_org_profiles_type
  ON org_profiles (type);

CREATE TABLE IF NOT EXISTS bill_profiles (
  bill_id               TEXT PRIMARY KEY,                        -- canonical, e.g. 's-1234-119', 'hr-5678-119' (chamber-number-congress)
  congress              INT NOT NULL,                            -- e.g. 119 for the 119th Congress
  title                 TEXT NOT NULL,                           -- official title from Congress.gov
  short_title           TEXT,                                    -- popular name, e.g. "Inflation Reduction Act"
  sponsor_bioguide      TEXT REFERENCES politician_profiles (bioguide_id) ON DELETE SET NULL,
  cosponsors            JSONB DEFAULT '[]'::jsonb,               -- ["A001234", "B001234", ...] of bioguide IDs
  status                TEXT,                                    -- 'introduced' | 'committee' | 'passed-chamber' | 'enacted' | 'vetoed' | 'failed'
  introduced_date       DATE,
  lobbying_for_usd      NUMERIC,                                 -- aggregate from OpenSecrets
  lobbying_against_usd  NUMERIC,
  external_links        JSONB DEFAULT '{}'::jsonb,               -- { govtrack, opensecrets, congress }
  notes                 TEXT,
  refreshed_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bill_profiles_sponsor
  ON bill_profiles (sponsor_bioguide);
CREATE INDEX IF NOT EXISTS idx_bill_profiles_congress
  ON bill_profiles (congress);
CREATE INDEX IF NOT EXISTS idx_bill_profiles_short_title_lower
  ON bill_profiles (LOWER(short_title));
