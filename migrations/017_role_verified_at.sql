-- Migration 017: record when a role source was last checked, and expire on it
--
-- WHY:
--   Migration 015 made every claim on an executive dossier carry the record
--   that backs it. It did not make that record's *currency* checkable, and for
--   one population the record decays.
--
--   The 13 published foreign-executive rows are sourced to a page that names
--   the person -- gov.uk naming Keir Starmer, pm.gc.ca naming Mark Carney.
--   That page is the only evidence they hold the office, and it stops being
--   true the moment they leave it. This is not hypothetical: gov.uk records
--   that Starmer was Prime Minister "from 5 July 2024 to 20 July 2026", and
--   at the time 015 shipped, Sift's own prose still called him the sitting
--   PM. Nothing in the system detected that, and nothing would have.
--
--   No such decay affects the other 56 published rows, which is why this
--   expires only `foreign-executive`:
--
--     US executive  role_title is a statute (10 U.S.C. 113 will still say
--                   "Secretary of Defense" in ten years) and the person is
--                   evidenced by a Senate roll-call, which is permanent and
--                   stays true -- "the Senate confirmed him on this date" does
--                   not expire. Departure is caught structurally instead: the
--                   successor's confirmation sets role_end_date
--                   (build_executive_profiles.py's `successor`).
--     Presidents /  role dates come from the National Archives' Electoral
--     Vice Pres.    College record, also permanent.
--     scotus        28 U.S.C. 1 plus a confirmation roll-call. Same shape.
--
--   Applying a blanket expiry to all 69 would drop 56 correct rows out of the
--   sitemap for no correctness reason, purely from nobody re-running a script.
--   The gate should bind where the risk is.
--
-- WHY EXPIRY RATHER THAN A CRON THAT RE-VERIFIES AND UNPUBLISHES:
--   The observed failure mode of these sources is bot-blocking, not office
--   change -- of the 46 foreign rows, 6 return hard 403s and 3 more render
--   their content in JS, which is most of why only 13 publish. A job that
--   wrote to prod whenever verification failed would be acting on a signal
--   that is roughly half infrastructure noise, and would remove Putin or
--   Macron from the index the first time a government site rate-limited us.
--
--   Expiry inverts that. Nothing writes on failure; a row simply ages out
--   unless someone re-runs verify_role_sources.py and re-seeds. Neglect
--   withholds rather than publishes, which is the direction this codebase
--   already chose -- `dossierRobotsMeta` emits `index: false, follow: true`
--   rather than hiding the page, and an expired row keeps rendering and keeps
--   resolving entity chips. It just stops being advertised.
--
-- PRECEDENT:
--   outlet_profiles.allsides_last_checked / mbfc_last_checked and
--   org_profiles.self_description_checked already record "when did we look".
--   The only new idea here is making that date load-bearing for publication
--   rather than decorative.
--
-- THE WINDOW IS 90 DAYS, and it is a judgement, not a derivation. Long enough
--   that a quarterly re-run keeps the set published; short enough that a head
--   of government who left office is withheld within one quarter rather than
--   indefinitely. It lives in ONE place per repo -- ROLE_VERIFICATION_MAX_AGE_DAYS
--   in sift/lib/publishFloor.ts, mirrored by the interval in listSitemapEntries.

ALTER TABLE politician_profiles
  ADD COLUMN IF NOT EXISTS role_verified_at DATE;

COMMENT ON COLUMN politician_profiles.role_verified_at IS
  'Date role_title_source was last refetched and confirmed to state role_title (and, where the row carries verify_name, to name the person). Written by scripts/seed_executive_records.py from scripts/verify_role_sources.py''s report -- the date of the CHECK, not of the write. Required, and required to be recent, for a foreign-executive row to publish; see migration 017 for why the other populations do not expire.';

-- Matches the publish gate in sift/lib/db.ts listSitemapEntries, which filters
-- chamber first and then requires a sourced role plus, for foreign rows, a
-- recent check.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_politician_profiles_role_verified
  ON politician_profiles (chamber, role_verified_at)
  WHERE role_title IS NOT NULL AND role_title_source IS NOT NULL;
