-- 017_entity_alias_match_case.sql
--
-- Let a curated alias opt into case-sensitive matching.
--
-- Why this exists. `ICE` is the most-mentioned agency in the corpus that the
-- linker cannot name: 1,725 articles carry it in uppercase, 1,386 of them
-- politics, and the dossier
-- (`us-immigration-and-customs-enforcement`) has existed the whole time. It
-- links in 8 of 300 sampled articles. The blocker is not curation — it is that
-- `_word_pattern` compiles every key with `re.IGNORECASE`, so the alias `ice`
-- would also fire on "ice cream", "sea ice", "on the ice" and the rest of the
-- 672 lowercase occurrences the corpus holds, most of them in the 85k sports
-- and 48k entertainment articles.
--
-- Casing settles it. Measured 2026-08-07 against prod:
--
--   whole-word ICE, case-sensitive     1,725 articles
--   ... of which Intercontinental
--       Exchange (the futures market)      5   business + energy desks
--   ... all-caps headlines (a shouted
--       title would defeat casing)         0
--
-- 15 of 15 randomly sampled uppercase occurrences were the agency. That is
-- ~99.7% precision against 0% today. The five Intercontinental Exchange
-- articles are the known cost and are recorded in the row's `notes`.
--
-- This is also the mechanism sift-api#151 asked for. That issue found
-- `variety` (42 of 67 stored chips), `the athletic` (64 of 123), `wired`
-- (19 of 79) and `the hill` (3 of 84) firing on the common noun, and noted
-- that a blocklist entry is the wrong fix because each ALSO has real,
-- correctly-cased third-party mentions that a blocklist would destroy — "the
-- fix they need is a casing rule, not a blocklist entry". This column is that
-- rule. It is not applied to those four here; each needs its own measurement
-- first, on the split-by-self-vs-third-party discipline `stat` established.
--
-- Default FALSE, so every one of the 96 existing rows keeps the
-- case-insensitive behaviour it was curated under. Opting in is per-row and
-- deliberate.

ALTER TABLE entity_aliases
  ADD COLUMN IF NOT EXISTS match_case BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN entity_aliases.match_case IS
  'When true, the linker matches this alias case-sensitively. For acronyms '
  'whose lowercase form is an ordinary word (ICE, and the #151 candidates). '
  'The stored `alias` keeps the casing to match on; every other row is '
  'lowercased as before.';
