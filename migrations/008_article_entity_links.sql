-- Article-to-entity linking for the civic-literacy MVP (Phase 3.G).
--
-- See plans/sift-civic-literacy.md (Phase 3, F4 inline glossary).
--
-- entity_links is a denormalized JSONB array on the articles table that
-- stores resolved references to politicians / orgs / bills / outlets
-- mentioned in the article's title + summary. Populated by the
-- entity_linker pipeline node (services/entity_linker.py) at ingest time.
--
-- Shape:
--   [
--     {"type": "politician", "canonical_id": "S000148", "surface_form": "Chuck Schumer"},
--     {"type": "org",        "canonical_id": "brookings-institution", "surface_form": "Brookings Institution"},
--     {"type": "bill",       "canonical_id": "hr-5376-117", "surface_form": "Inflation Reduction Act"}
--   ]
--
-- The frontend (sift Phase 3.H InlineGlossaryTooltip) reads this array
-- to render hover/tap context panels and link-outs to the dossier
-- routes (/politician/[bioguide], /org/[slug], /bill/[id]).
--
-- Stored on the article (denormalized) rather than as a join table for
-- read-path simplicity: every article fetch from the feed already
-- returns this column inline; no extra JOIN needed in sift/lib/db.ts.
-- The schema is small and the column rarely shrinks, so the storage
-- cost is acceptable.
--
-- GIN index supports "find every article that mentions a given entity"
-- queries — Phase 3.H's dossier "recent articles" sections, and
-- analytics later.

ALTER TABLE articles
  ADD COLUMN IF NOT EXISTS entity_links JSONB DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_articles_entity_links_gin
  ON articles USING gin(entity_links);
