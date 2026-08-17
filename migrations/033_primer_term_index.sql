-- 033_primer_term_index.sql
--
-- Lets `/term/<slug>` count the articles whose primer defines a term, not
-- only the ones that put it in the headline.
--
-- The gap this closes
-- -------------------
-- The coverage query added in 032 searches `title || summary`. Measured
-- against the corpus, that misses precisely the terms most worth a page,
-- because they are what a journalist writes in paragraph nine:
--
--     term                title+summary   primer definitions
--     prior restraint                 0                  128
--     certiorari                      5                   83
--     qualified immunity              6                   75
--     cloture                         0                   45
--
-- `prior-restraint` was therefore withheld from the sitemap for having "no
-- coverage" when 128 articles were about it. The floor was working; the
-- signal under it was incomplete.
--
-- Why the primer is a legitimate coverage signal — and still not a definition
-- --------------------------------------------------------------------------
-- Two different questions, and the answer differs for each:
--
--   "What does this term mean?"    The primer CANNOT answer it here. All
--                                  72,689 primer terms in the corpus have
--                                  `source: null`, which is why 031 created a
--                                  hand-sourced table instead.
--   "Which articles involve it?"   The primer answers this well. It is
--                                  Sift's own reading of its own corpus —
--                                  reportage about the index, needing no
--                                  citation beyond the articles it links.
--
-- It is also the *higher-precision* signal of the two. The primer generator
-- reads the article; the regex does not. #40 is the standing reminder of what
-- a context-free matcher does to a corpus ("the nation's fuel" -> the-nation,
-- 1,523 articles), and it points the same way here.
--
-- Why a function rather than plain jsonb containment
-- --------------------------------------------------
-- `context_primer->'terms' @> '[{"term":"..."}]'` is GIN-indexable but
-- case-SENSITIVE, and the generator is not consistent: 'redistricting' appears
-- 483 times and 'Redistricting' twice. Folding case inside an IMMUTABLE
-- function and indexing that keeps the match case-insensitive AND indexed.
--
-- The function is total: NULL in, NULL out (STRICT); a missing, non-array or
-- malformed `terms` yields an empty array rather than an error, so a bad
-- primer row cannot break the read path.

CREATE OR REPLACE FUNCTION primer_term_keys(p jsonb)
RETURNS text[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $fn$
  SELECT COALESCE(array_agg(DISTINCT lower(btrim(x->>'term'))), ARRAY[]::text[])
    FROM jsonb_array_elements(
           CASE WHEN jsonb_typeof(p->'terms') = 'array' THEN p->'terms' ELSE '[]'::jsonb END
         ) x
   WHERE jsonb_typeof(x) = 'object'
     AND NULLIF(btrim(x->>'term'), '') IS NOT NULL
$fn$;

-- Matched with `&&` (array overlap) in sift/lib/db.ts. Measured: 0.2 ms and
-- 82 heap blocks for a term with 83 articles, against a full seq scan without
-- it. 2.6 MB over 97,328 articles that carry a primer terms array.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_primer_terms
  ON articles USING gin (primer_term_keys(context_primer));
