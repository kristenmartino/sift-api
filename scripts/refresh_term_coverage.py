"""Recompute term_profiles' coverage counts (migration 034).

Run from sift-api root:
    railway run ./.venv/bin/python3 scripts/refresh_term_coverage.py --dry-run
    railway run ./.venv/bin/python3 scripts/refresh_term_coverage.py

Run it after seeding or editing terms, and on the seeders' cadence
thereafter. `/glossary` and the publish floor read what this writes.

Why the counts are stored at all
--------------------------------
"Which articles involve this term" is a lateral over term x surface-form x
corpus. Measured against prod as the table grew: 785 ms at 24 terms, 1,522 ms
at 37 -- and the slope steepens, because more terms means more matched rows to
aggregate. At ~100 terms that is several seconds on a page regeneration.

Only /glossary and the sitemap floor scale that way. `/term/<slug>` is
per-term, stays at 34-81 ms whatever the table size, and deliberately keeps
computing live -- so the per-term page is never stale even when the index is.

This script is the single definition of coverage
------------------------------------------------
The query below and `termMatchSql` in sift/lib/db.ts must agree. They are
separate implementations because one aggregates the whole table and the other
filters for one term, and `__tests__/term.test.ts` plus the checks here pin the
shared rules:

  * an article counts when the term is in title/summary OR the article's own
    primer defined it (migration 033);
  * all-caps surface forms match case-sensitively (isAcronym), everything else
    case-insensitively;
  * word boundaries are POSIX \\m and \\M, never \\b -- Postgres reads \\b as a
    backspace, which silently turns word matching into substring matching.

If those drift apart, /glossary and the term page disagree about the same
number in front of the same reader.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings

# Mirrors sift/lib/db.ts GLOSSARY_QUERY. `bool_or` per (term, article) so an
# article naming the term through ANY of its surface forms counts as named --
# a story that writes "TPS" but never "Temporary Protected Status" is not an
# example of coverage failing to name the term.
COVERAGE_SQL = r"""
WITH per AS (
  SELECT t.slug, a.id, a.source_name,
         bool_or(
           to_tsvector('english', COALESCE(a.title,'') || ' ' || COALESCE(a.summary,''))
             @@ phraseto_tsquery('english', f.s)
           AND CASE WHEN f.s = UPPER(f.s)
                    THEN (COALESCE(a.title,'') || ' ' || COALESCE(a.summary,'')) ~
                         ('\m' || regexp_replace(f.s, '([.*+?^$(){}|\[\]\\])', '\\\1', 'g') || '\M')
                    ELSE (COALESCE(a.title,'') || ' ' || COALESCE(a.summary,'')) ~*
                         ('\m' || regexp_replace(f.s, '([.*+?^$(){}|\[\]\\])', '\\\1', 'g') || '\M')
               END
         ) AS in_text
    FROM term_profiles t
    CROSS JOIN LATERAL (
      SELECT t.term AS s UNION SELECT jsonb_array_elements_text(t.aliases)
    ) f
    JOIN articles a
      ON a.from_search = false
     AND a.summary IS NOT NULL AND a.summary <> ''
     AND LOWER(a.summary) NOT LIKE 'unable to provide%'
     AND ( (to_tsvector('english', COALESCE(a.title,'') || ' ' || COALESCE(a.summary,''))
              @@ phraseto_tsquery('english', f.s)
            AND CASE WHEN f.s = UPPER(f.s)
                     THEN (COALESCE(a.title,'') || ' ' || COALESCE(a.summary,'')) ~
                          ('\m' || regexp_replace(f.s, '([.*+?^$(){}|\[\]\\])', '\\\1', 'g') || '\M')
                     ELSE (COALESCE(a.title,'') || ' ' || COALESCE(a.summary,'')) ~*
                          ('\m' || regexp_replace(f.s, '([.*+?^$(){}|\[\]\\])', '\\\1', 'g') || '\M')
                END)
           OR primer_term_keys(a.context_primer) && ARRAY[lower(btrim(f.s))] )
   WHERE t.definition IS NOT NULL AND t.definition_source IS NOT NULL
   GROUP BY t.slug, a.id, a.source_name
)
SELECT p.slug,
       COUNT(*)::int AS article_count,
       COUNT(DISTINCT COALESCE(sa.outlet_slug, op.slug, LOWER(p.source_name)))::int AS outlet_count,
       COUNT(*) FILTER (WHERE NOT p.in_text)::int AS unnamed_count
  FROM per p
  LEFT JOIN source_name_aliases sa ON LOWER(sa.raw_source_name) = LOWER(p.source_name)
  LEFT JOIN outlet_profiles op ON LOWER(op.name) = LOWER(p.source_name)
 GROUP BY p.slug
"""

# Mirrors TERM_MIN_ARTICLES in sift/lib/publishFloor.ts. Reported, not applied
# -- this script measures, the floor decides.
TERM_MIN_ARTICLES = 8


async def main(dry_run: bool) -> int:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl = "require" if "neon.tech" in db_url else False
    conn = await asyncpg.connect(db_url, ssl=ssl, command_timeout=900)
    try:
        before = {
            r["slug"]: (r["article_count"], r["coverage_computed_at"])
            for r in await conn.fetch(
                "SELECT slug, article_count, coverage_computed_at FROM term_profiles"
            )
        }
        rows = await conn.fetch(COVERAGE_SQL)
        measured = {r["slug"]: r for r in rows}

        # A sourced term with no matching article produces no row above. It
        # must still be written -- as an explicit 0 with a fresh stamp, not
        # left NULL, or "measured and found nothing" is indistinguishable from
        # "never measured" and the term is withheld forever.
        sourced = [
            r["slug"] for r in await conn.fetch(
                "SELECT slug FROM term_profiles "
                " WHERE definition IS NOT NULL AND definition_source IS NOT NULL"
            )
        ]
        payload = []
        for slug in sourced:
            m = measured.get(slug)
            payload.append((
                slug,
                m["article_count"] if m else 0,
                m["outlet_count"] if m else 0,
                m["unnamed_count"] if m else 0,
            ))

        print(f"Sourced terms measured: {len(payload)}")
        never = [s for s, *_ in payload if before.get(s, (None, None))[1] is None]
        if never:
            print(f"  first measurement for {len(never)}: {', '.join(sorted(never)[:6])}"
                  + (" ..." if len(never) > 6 else ""))

        moved = [
            (s, before[s][0], n) for s, n, _, _ in payload
            if s in before and before[s][0] is not None and before[s][0] != n
        ]
        if moved:
            print(f"\nCounts that moved ({len(moved)}):")
            for s, old, new in sorted(moved, key=lambda x: -abs(x[2] - x[1]))[:12]:
                print(f"  {s:<34}{old:>6} -> {new:<6} ({new - old:+})")

        crossings = [
            (s, before[s][0], n) for s, n, _, _ in payload
            if s in before and before[s][0] is not None
            and (before[s][0] >= TERM_MIN_ARTICLES) != (n >= TERM_MIN_ARTICLES)
        ]
        if crossings:
            print(f"\nFloor crossings ({len(crossings)}) -- these change what is indexed:")
            for s, old, new in crossings:
                direction = "now PUBLISHES" if new >= TERM_MIN_ARTICLES else "now WITHHELD"
                print(f"  {s:<34}{old} -> {new}  {direction}")

        below = [s for s, n, *_ in payload if n < TERM_MIN_ARTICLES]
        print(f"\nAbove floor: {len(payload) - len(below)}/{len(payload)}")
        if below:
            print(f"  below: {', '.join(sorted(below))}")

        if dry_run:
            print("\n--dry-run set; no DB writes.")
            return 0

        async with conn.transaction():
            await conn.executemany(
                """
                UPDATE term_profiles
                   SET article_count        = $2,
                       outlet_count         = $3,
                       unnamed_count        = $4,
                       coverage_computed_at = NOW()
                 WHERE slug = $1
                """,
                payload,
            )
        print(f"\nWrote counts for {len(payload)} terms.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Recompute term coverage counts.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
