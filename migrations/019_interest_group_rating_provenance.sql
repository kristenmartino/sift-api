-- 019_interest_group_rating_provenance.sql
--
-- Reshape politician_profiles.interest_group_ratings so a rating cannot be
-- stored without the year and the URL that back it.
--
-- The old shape was a bare dictionary:
--
--     { "LCV": 92, "NRA": "F", "ADA": 88, "ACU": 6 }
--
-- which is a claim about how a named, living person voted, with no year and
-- no citation. That is the same defect migration 013 removed from
-- org_profiles (`notes`, `annual_budget_usd`) and migration 015 removed from
-- the executive rows (`notes`) — this would have been the third instance.
-- It was never populated: empty on all 647 rows since the column was created,
-- so there is no data to migrate, only a shape to fix before anything lands.
--
-- New shape — an array, one object per rater:
--
--     [
--       {
--         "rater":          "LCV",
--         "rater_name":     "League of Conservation Voters",
--         "score":          20,
--         "unit":           "percent",
--         "year":           2025,
--         "lifetime_score": 20,
--         "source_url":     "https://www.lcv.org/moc/lisa-a-murkowski/"
--       }
--     ]
--
-- `score`, `year` and `source_url` are all required; lib/politician.ts drops
-- any entry missing one, the same way lib/org.ts nulls the budget triple.
-- `lifetime_score` is optional.
--
-- **These are not Sift's assessments.** Each entry is one advocacy group's own
-- published number, attributed and linked — the treatment outlet_profiles
-- already gives AllSides and MBFC. The UI must name the rater and the year;
-- it must not average raters, derive a composite, or present a single rater
-- as a general "rating".
--
-- Array, not object, deliberately: two raters can score the same member in
-- different years, and a dictionary keyed by rater cannot hold that.

ALTER TABLE politician_profiles
  ALTER COLUMN interest_group_ratings SET DEFAULT '[]'::jsonb;

-- Every existing value is the empty object the old default produced. Convert
-- to the empty array so nothing reads a dict where the parser expects a list.
UPDATE politician_profiles
   SET interest_group_ratings = '[]'::jsonb
 WHERE interest_group_ratings IS NULL
    OR jsonb_typeof(interest_group_ratings) <> 'array';

COMMENT ON COLUMN politician_profiles.interest_group_ratings IS
  'Array of third-party scorecard entries. Each: {rater, rater_name, score, '
  'unit, year, lifetime_score?, source_url}. score+year+source_url required — '
  'an uncited rating about a living person must not be stored. Values are the '
  'rating organization''s own published numbers, never Sift''s assessment.';
