-- 014_entity_aliases.sql
--
-- A curated surface-form → dossier map for the entity linker.
--
-- Why this exists. The 2026-08-05 audit (scripts/audit_unlinked_entities.py)
-- found only 21,614 of 282,931 articles carry any entity_links at all — 7.6%.
-- A large share of the miss is not missing dossiers, it is missing *names* for
-- dossiers that already exist:
--
--     "Pentagon"        756 articles, no chip → united-states-department-of-defense
--     "Democrats"       888 articles, no chip → (party dossier, once one exists)
--     "Knicks"        1,137 articles, no chip → "New York Knicks"
--
-- services/entity_linker.py matches the full canonical name only. That policy
-- is deliberate and correct as a *default* — see politician_aliases(), which
-- documents why mechanically-derived last-name aliases were removed in #40
-- (common-noun surnames like Cloud, Self, Banks, Hill false-match constantly).
-- The lesson there was that *derived* aliases are unsafe, not that aliases are.
-- This table is the curated alternative that function's docstring anticipated.
--
-- Shape follows source_name_aliases (migration 006), which already does exactly
-- this job for outlet source_name strings.
--
-- Editorial note: an alias is a claim that two names denote the same entity.
-- `notes` carries the justification for anything non-obvious, in keeping with
-- the rule that a dossier asserts nothing it cannot source.

CREATE TABLE IF NOT EXISTS entity_aliases (
  -- Lowercased surface form. Globally unique on purpose: build_search_dict
  -- drops any key with conflicting refs anyway, so an alias that could mean
  -- two things is worthless. Enforcing it here surfaces the conflict at
  -- curation time instead of silently at link time.
  alias         TEXT PRIMARY KEY,

  entity_type   TEXT NOT NULL
                CHECK (entity_type IN ('politician', 'org', 'bill', 'outlet')),

  -- No FK: the four dossier tables have four different PK columns, so this
  -- cannot be a single reference. seed_entity_aliases.py validates against
  -- the live tables and refuses to insert an unresolvable target.
  canonical_id  TEXT NOT NULL,

  notes         TEXT,
  added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_target
  ON entity_aliases (entity_type, canonical_id);
