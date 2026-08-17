-- 032_articles_fulltext.sql
--
-- Full-text index over article title + summary, so `/term/<slug>` can ask
-- "which articles mention this term" without reading the whole corpus.
--
-- Measured before this existed, against 305,261 prod articles: a word-boundary
-- regex over `title || ' ' || summary` is a parallel seq scan every time —
-- 1,000-1,800 ms and ~52,000 buffers per term, most of it read from disk. Fine
-- for one page behind ISR; not fine at the ~992 terms that appear in 8+
-- articles, and exactly the kind of invisible standing load D54 is about.
--
-- Why this index does NOT change what the page claims
-- ---------------------------------------------------
-- Postgres FTS stems: `phraseto_tsquery('english', 'temporary protected
-- status')` becomes `temporari <-> protect <-> status`, which is looser than
-- the phrase. So the read query uses this index as a **prefilter only** and
-- keeps the exact regex as the confirming predicate:
--
--     WHERE to_tsvector('english', title||' '||summary) @@ phraseto_tsquery(...)
--       AND (title||' '||summary) ~* '\mtemporary protected status\M'
--
-- The index narrows 305k rows to a few hundred; the regex decides. Match
-- semantics are identical to the pre-index behaviour — this is a speed change,
-- not a meaning change, and `lib/db.ts` carries the same note so nobody
-- "simplifies" it by dropping the regex.
--
-- The expression must match `termCoverageWhere` in sift/lib/db.ts character
-- for character, or Postgres will not use the index at all.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_articles_fulltext
  ON articles
  USING gin (to_tsvector('english',
              COALESCE(title, '') || ' ' || COALESCE(summary, '')));
