-- Migration 016: Supreme Court Justices join politician_profiles; judge_profiles retires
--
-- WHY THIS TABLE EXISTED AT ALL:
--   `judge_profiles` was created by sift-api commit 9f44ba2 ("Phase 3.J",
--   2026-05-20) on branch `state-mgmt-setup`, as `migrations/011_judge_profiles.sql`.
--   That branch never merged. Its seeders were nonetheless run against prod by
--   hand the same day — all 9 rows carry `updated_at = 2026-05-20 07:24:32.733575`,
--   one transaction, ~50 minutes before the commit was authored. The same run
--   produced the 56 `executive` + 46 `foreign-executive` politician rows and the
--   93 `agency` org rows that are still live.
--
--   So the table exists in production and in no repo. `main` never had it:
--   migration number 011 was taken by `011_ai_usage_daily.sql`, `init.sql` never
--   described it, `_apply_migrations` never created it, and `main`'s linker has
--   no `judge` type — the 87 judge entity_links across 51 articles were written
--   by branch code run manually and cannot be refreshed. `sift/lib/entityLinks.ts`
--   drops unknown types, so they have never rendered on the web. The only live
--   consumer was `sift-mcp`, which this change also retires.
--
-- WHY politician_profiles RATHER THAN KEEPING THE TABLE:
--   Migration 015 already decided this, eleven weeks later than the branch and
--   with the cost in view. Its `id_source` comment enumerates
--   'bioguide' | 'executive' | 'foreign-executive' | 'scotus' — the slot was
--   reserved. And 015 states the reason it refused a separate `official_profiles`
--   table for executives: a new table needs a new `entity_links.type` value,
--   "a breaking change across sift/lib/entityLinks.ts, types.ts,
--   EntityChipTooltip.tsx, CivicIndex.tsx and entity_linker_llm.py's roster
--   headings." That applies to `judge` verbatim, plus one cost 015 did not have
--   to pay: `entity_aliases` CHECKs entity_type IN (politician, org, bill,
--   outlet), so a fifth type needs a constraint migration too.
--
--   The branch's stated justification — "the metadata shape diverges (court,
--   nominating president, confirmation year vs party/state/chamber)" — was true
--   on 2026-05-20 and is not true now. 015 added role_title, role_start_date,
--   nomination_date, confirmation_date, confirmation_vote_url,
--   confirmation_vote_result and predecessor_name to politician_profiles.
--   Justices fit that shape better than executives do: every sitting Justice has
--   a senate.gov confirmation roll-call.
--
-- WHAT IS NOT CARRIED OVER:
--   `notes`. All 9 rows held uncited characterizations of living people —
--   "Confirmed 50-48 after contested hearings", "Authored the Dobbs majority
--   opinion (2022)", "First African American woman on the Supreme Court" —
--   with Wikipedia in external_links. That is the defect migration 013 removed
--   from org_profiles and 015 removed from the 102 executive rows, surviving
--   here only because nothing in the repo knew the table existed.
--   OPERATING_CONTEXT.md §5 forbids it outright: no dossier claim about a
--   living person without a citation to the primary record, and no
--   characterization of a legal outcome beyond what that record literally says.
--
--   The `wikipedia` key is stripped from external_links for the same reason
--   STATUS.md:109-113 gave when it dropped org `founded_year` rather than cite
--   Wikipedia: "one unsourced field on an otherwise fully-sourced page is the
--   field a reader spot-checks precisely because everything else is cited."
--
--   Also dropped rather than migrated: `senior_status_year` (inapplicable —
--   no sitting Justice has taken senior status; the concept is 28 U.S.C. § 371
--   retirement, which for a Justice means leaving the Court), and
--   `previous_positions`, which was uncited prose in JSONB.
--
-- WHAT REPLACES IT, AND WHERE EACH CLAIM COMES FROM:
--   role_title               <- 28 U.S.C. § 1 (govinfo.gov/GPO), which states
--                               the Court "shall consist of a Chief Justice of
--                               the United States and eight associate justices"
--   role_title_source        <- that URL
--   confirmation_date        <- senate.gov roll-call vote menu
--   confirmation_vote_result <- same, verbatim tally
--   confirmation_vote_url    <- same, the roll-call page itself
--
--   Gathered by scripts/scrape_executive_records.py + build_scotus_records.py
--   into data/scotus_confirmations.csv, applied by seed_scotus_records.py.
--   Every constructed roll-call URL is fetched and required to name that
--   Justice before the row is written — senate.gov answers 200 for any vote
--   number that exists, so the status code alone proves nothing.
--
--   `nomination_date` / `nomination_url` / `predecessor_name` are left NULL:
--   they need api.congress.gov, which without a CONGRESS_API_KEY is limited to
--   ~10 requests/hour. A NULL column simply does not render (013's pattern);
--   an unsourced one would.
--
-- id_source = 'scotus' AND chamber = 'scotus'. Both, redundantly, matching how
--   the executive rows set chamber = 'executive' AND id_source = 'executive'.
--   chamber is what every dossier query and the sitemap gate filter on first;
--   id_source is what records that the PK is a synthetic Sift id.

-- The INSERT is conditional: on a fresh DB built from init.sql, judge_profiles
-- has never existed, and this migration must still be a no-op rather than an
-- error. On prod it moves the 9 rows.
DO $$
BEGIN
  IF to_regclass('public.judge_profiles') IS NOT NULL THEN
    INSERT INTO politician_profiles (bioguide_id, name, chamber, id_source,
                                     external_links, refreshed_at, updated_at)
    SELECT canonical_id,
           name,
           'scotus',
           'scotus',
           COALESCE(external_links, '{}'::jsonb) - 'wikipedia',
           refreshed_at,
           updated_at
      FROM judge_profiles
    ON CONFLICT (bioguide_id) DO NOTHING;

    DROP TABLE judge_profiles;
  END IF;
END $$;

COMMENT ON COLUMN politician_profiles.chamber IS
  'Which body the person sits in: ''senate'' | ''house'' | ''former'' | ''executive'' | ''foreign-executive'' | ''scotus''. Every dossier query and sift/lib/db.ts listSitemapEntries filter on this first.';
