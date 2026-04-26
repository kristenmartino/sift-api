"""
Diagnostic: run EXPLAIN (ANALYZE, BUFFERS) against the /api/news feed queries
for every category and flag regressions.

Run from sift-api root:
    python scripts/explain_feed_queries.py           # summary table
    python scripts/explain_feed_queries.py --verbose # full plans

Thresholds (picked against the 10s client-side API_TIMEOUT_MS in
sift/lib/constants.ts):
    < WARN_MS   → ok
    < FAIL_MS   → warn
    >= FAIL_MS  → fail (non-zero exit code, suitable for CI)

The SQL mirrors sift/lib/db.ts exactly:
    - getStoriesWithArticles stories query  (lib/db.ts:85)
    - getStoriesWithArticles standalone query (lib/db.ts:150)
    - getArticlesByCategory (lib/db.ts:36)
"""
import argparse
import asyncio
import json
import os
import sys

# Let the script run from any CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from app.config import settings


CATEGORIES = [
    "top", "technology", "business", "science", "energy",
    "world", "health", "politics", "sports", "entertainment",
]

WARN_MS = 2_000
FAIL_MS = 8_000

# NOTE: these are passed through EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON).
# Keep them byte-identical to the live queries in sift/lib/db.ts.

STORIES_SQL = """
SELECT s.id, s.headline, s.summary, s.category, s.framings, s.entities,
       COUNT(a.id)::int AS article_count,
       s.representative_image_url, s.published_date, s.synthesis_status
FROM stories s
LEFT JOIN articles a
  ON a.story_id = s.id
  AND a.from_search = false
  AND a.summary IS NOT NULL AND a.summary != ''
  AND LOWER(a.summary) NOT LIKE 'unable to provide%'
WHERE s.category = $1 AND s.synthesis_status = 'complete'
GROUP BY s.id
HAVING COUNT(a.id) >= 2
ORDER BY
  COUNT(a.id)::float *
  EXP(-LEAST(EXTRACT(EPOCH FROM (NOW() - COALESCE(s.published_date, s.created_at))) / 86400.0, 700))
DESC NULLS LAST
LIMIT 20
"""

STANDALONE_SQL = """
SELECT id, title, summary, source_url, source_name, image_url,
       category, published_date, read_time, why_it_matters, importance_score, created_at
FROM articles
WHERE category = $1 AND from_search = false
  AND (story_id IS NULL OR story_id <> ALL($2::text[]))
  AND summary IS NOT NULL AND summary != ''
  AND LOWER(summary) NOT LIKE 'unable to provide%'
ORDER BY
  COALESCE(importance_score, 3)::float *
  EXP(-LEAST(EXTRACT(EPOCH FROM (NOW() - COALESCE(published_date, created_at))) / 86400.0, 700))
DESC NULLS LAST
LIMIT 50
"""

ARTICLES_SQL = """
SELECT id, title, summary, source_url, source_name, image_url,
       category, published_date, read_time, why_it_matters, importance_score, created_at
FROM articles
WHERE category = $1 AND from_search = false
  AND summary IS NOT NULL AND summary != ''
  AND LOWER(summary) NOT LIKE 'unable to provide%'
ORDER BY
  COALESCE(importance_score, 3)::float *
  EXP(-LEAST(EXTRACT(EPOCH FROM (NOW() - COALESCE(published_date, created_at))) / 86400.0, 700))
DESC NULLS LAST
LIMIT 30
"""


def find_indexes(plan_node: dict) -> set[str]:
    """Walk a plan JSON tree and collect every Index Name mentioned."""
    names: set[str] = set()
    if "Index Name" in plan_node:
        names.add(plan_node["Index Name"])
    for child in plan_node.get("Plans", []):
        names.update(find_indexes(child))
    return names


def has_seq_scan(plan_node: dict) -> bool:
    node_type = plan_node.get("Node Type", "")
    if node_type in {"Seq Scan", "Parallel Seq Scan"}:
        return True
    return any(has_seq_scan(c) for c in plan_node.get("Plans", []))


async def explain(conn: asyncpg.Connection, sql: str, params: list) -> dict:
    """Return the root plan dict from EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)."""
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"
    row = await conn.fetchval(explain_sql, *params)
    # asyncpg gives us the JSON as a Python str; some drivers return parsed
    # already. Handle both.
    if isinstance(row, str):
        data = json.loads(row)
    else:
        data = row
    return data[0]["Plan"], data[0]["Execution Time"]


def verdict(ms: float) -> str:
    if ms >= FAIL_MS:
        return "FAIL"
    if ms >= WARN_MS:
        return "WARN"
    return "ok"


async def run(verbose: bool) -> int:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    conn = await asyncpg.connect(db_url, ssl=ssl_mode)

    worst_verdict = "ok"
    header = f"{'category':<14} {'query':<11} {'ms':>9}  {'indexes used':<50} status"
    print(header)
    print("-" * len(header))

    try:
        for cat in CATEGORIES:
            # stories first so we can reuse the ids for the standalone query
            stories_plan, stories_ms = await explain(conn, STORIES_SQL, [cat])
            stories_indexes = find_indexes(stories_plan)

            # standalone reuses the story-id list. We don't have the real list
            # cheaply without re-running the stories query as data; passing an
            # empty array exercises the same index predicates and matches
            # actual plans well enough for regression detection.
            standalone_plan, standalone_ms = await explain(
                conn, STANDALONE_SQL, [cat, []]
            )
            standalone_indexes = find_indexes(standalone_plan)

            articles_plan, articles_ms = await explain(conn, ARTICLES_SQL, [cat])
            articles_indexes = find_indexes(articles_plan)

            for label, ms, idx, plan in [
                ("stories", stories_ms, stories_indexes, stories_plan),
                ("standalone", standalone_ms, standalone_indexes, standalone_plan),
                ("articles", articles_ms, articles_indexes, articles_plan),
            ]:
                v = verdict(ms)
                if v == "FAIL":
                    worst_verdict = "FAIL"
                elif v == "WARN" and worst_verdict != "FAIL":
                    worst_verdict = "WARN"

                idx_str = ", ".join(sorted(idx)) if idx else "(no index — seq scan)"
                print(f"{cat:<14} {label:<11} {ms:>9.1f}  {idx_str:<50} {v}")

                # Surface warns/fails on the GitHub Actions Checks tab so a
                # slope of degradation is visible before it becomes a cliff.
                # https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions
                if v == "WARN":
                    print(
                        f"::warning title=feed-perf {cat}/{label} slow"
                        f"::{ms:.1f} ms (warn threshold {WARN_MS} ms). "
                        f"See sift-api#16 for the deferred follow-ups."
                    )
                elif v == "FAIL":
                    print(
                        f"::error title=feed-perf {cat}/{label} failing"
                        f"::{ms:.1f} ms (fail threshold {FAIL_MS} ms). "
                        f"Pick from sift-api#16."
                    )

                if verbose:
                    print(json.dumps(plan, indent=2))
                    print()

        print()
        print(f"worst: {worst_verdict}  (warn >= {WARN_MS} ms, fail >= {FAIL_MS} ms)")
        return 0 if worst_verdict != "FAIL" else 1
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose", action="store_true",
        help="Dump the full EXPLAIN JSON for each query.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.verbose)))


if __name__ == "__main__":
    main()
