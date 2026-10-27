-- Retain exact event identity when synthesis rewrites a story headline.
-- An alias belongs to the same story; this never merges or fuzzy-matches events.
ALTER TABLE stories ADD COLUMN IF NOT EXISTS headline_aliases TEXT[] NOT NULL DEFAULT '{}';

CREATE OR REPLACE FUNCTION preserve_story_headline_alias() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.headline IS DISTINCT FROM NEW.headline THEN
        NEW.headline_aliases := ARRAY(
            SELECT DISTINCT alias FROM unnest(
                COALESCE(NEW.headline_aliases, '{}'::text[]) || ARRAY[LOWER(OLD.headline)]
            ) AS alias
        );
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS preserve_story_headline_alias ON stories;
CREATE TRIGGER preserve_story_headline_alias BEFORE UPDATE OF headline ON stories
    FOR EACH ROW EXECUTE FUNCTION preserve_story_headline_alias();

-- The two candidate paths use story_id and exact-title indexes before any
-- publisher/date filtering, instead of rescanning all recent article bodies.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_title_lower ON articles (LOWER(title));
