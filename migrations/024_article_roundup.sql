-- Roundup/brief-container flag (ranking v2 stage 5).
-- Set at store time by services/genre.py:detect_roundup from title patterns
-- (program episodes, dated show titles, named daily briefs). Evidence: the
-- second labeled ranking eval (#204) — containers inherit importance from
-- the events their summaries mention while not being stories; the original
-- doom-feed pinned #1 was a CBS show episode.
--
-- DEFAULT FALSE, not nullable: no marker IS the not-a-container verdict.
-- Consumed by the read path: roundups rank ×0.4 and never take the hero.
--
-- Same DDL applied at startup by app/db.py:_apply_migrations.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS is_roundup BOOLEAN NOT NULL DEFAULT FALSE;
