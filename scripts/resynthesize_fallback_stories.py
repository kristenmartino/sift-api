"""
One-shot repair of stories that stored a synthesis *fallback* as 'complete'.

Run from sift-api root:
    python scripts/resynthesize_fallback_stories.py --dry-run
    python scripts/resynthesize_fallback_stories.py --apply
    python scripts/resynthesize_fallback_stories.py --dry-run --days 30

`services/story_synthesizer.synthesize_story` degrades rather than raising: on
an API error, timeout or unparseable response it returns `_fallback()` — the
first member article's own title and summary, no framings, flagged `_failed`.
`workflows/story_workflow.py:246` reads that flag and stores
`synthesis_status='failed'`, but `incremental_threading._attach` and `_create`
wrote `'complete'` unconditionally. From the #161 cutover until 2026-08-11,
every synthesis failure on the live path therefore became a *finished* story
carrying one outlet's headline and an empty `framings` array — rendered by the
feed under "how N outlets covered this", and never revisited.

Measured against prod 2026-08-11: **4 rows**, all created 2026-08-10, all
inside the 7-day feed window, 10-18 outlets each.

THE SIGNATURE
-------------
A stored fallback is identifiable without a flag on the row:

    synthesis_status = 'complete'
  AND framings = []                         -- a real synthesis has one per outlet
  AND headline = some member article's title -- `_fallback` copies articles[0]

All three are required. Empty framings alone is not enough — that is also what
a legitimately-degraded-but-honest row looks like — and a headline coincidence
alone is not enough either, since a good synthesis may echo an outlet's wording.
Verified on prod: **zero** rows have empty framings *without* a matching member
title, so on this data the conjunction has no false positives.

WHAT IT WRITES
--------------
Re-runs `synthesize_story` over each story's current members and overwrites
headline, summary and framings. `synthesis_status` is left at 'complete', which
it already is — these rows are visible in the feed right now, and the repair
makes them correct rather than hiding them.

A row whose re-synthesis *also* fails is reported and skipped, not written.
Downgrading it to 'failed' would pull a live story out of the feed, and
**nothing retries 'failed'** on the incremental path (`story_workflow.py:226`
is its only reader, and `pipeline_workflow.py:459` makes the two threading
paths mutually exclusive) — so it would stay dark. Re-run the script instead.

Cost: one `story_synthesizer.synthesize` call per row, ~$0.0022 each.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from app.config import settings
from services.story_synthesizer import synthesize_story

# `_fallback` copies articles[0]'s title verbatim, so the headline of a stored
# fallback is exactly a member's title and its framings array is empty. See
# the module docstring for why all three conditions are load-bearing.
SIGNATURE = """
    s.synthesis_status = 'complete'
    AND jsonb_array_length(COALESCE(s.framings, '[]'::jsonb)) = 0
    AND EXISTS (
        SELECT 1 FROM articles a WHERE a.story_id = s.id AND a.title = s.headline
    )
"""


def describe(url: str) -> str:
    """host/dbname, credentials stripped."""
    p = urlparse(re.sub(r"^postgres(ql)?\+\w+://", "postgresql://", url))
    return f"{p.hostname or '?'}:{p.port or 5432}/{(p.path or '/?').lstrip('/')}"


async def find_damaged(conn, days: int | None) -> list[dict]:
    window = "AND s.created_at > NOW() - ($1 || ' days')::interval" if days else ""
    rows = await conn.fetch(
        f"""SELECT s.id, s.category, s.headline, s.article_count, s.created_at,
                   (SELECT count(DISTINCT a.source_name)
                      FROM articles a WHERE a.story_id = s.id) AS outlets
              FROM stories s
             WHERE {SIGNATURE} {window}
             ORDER BY s.created_at""",
        *([str(days)] if days else []),
    )
    return [dict(r) for r in rows]


async def members_of(conn, story_id: str) -> list[dict]:
    return [dict(r) for r in await conn.fetch(
        """SELECT id, title, summary, source_name, source_url
             FROM articles WHERE story_id = $1 ORDER BY published_date""",
        story_id,
    )]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--days", type=int, default=None,
        help="only rows created within N days (default: all of them)",
    )
    parser.add_argument(
        "--backup",
        default=os.path.expanduser("~/sift-backups/fallback_stories_backup.json"),
        help="pre-write snapshot of every row --apply touches",
    )
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip() or settings.database_url

    # ALWAYS name the target before touching it. `app.config.settings` reads
    # `sift-api/.env`, so running this from a git worktree — which has no
    # `.env` — silently falls back to the *local docker* database and reports
    # "nothing to repair" against stale data. That is the pipeline-honesty
    # failure class in STATUS.md, in a repair script. Set DATABASE_URL
    # explicitly, or run from the repo root that owns the .env.
    print(f"target: {describe(url)}\n")

    conn = await asyncpg.connect(
        url, **({"ssl": "require"} if "neon.tech" in url else {})
    )
    try:
        damaged = await find_damaged(conn, args.days)
        if not damaged:
            print("no stored fallbacks found — nothing to repair "
                  f"(on {describe(url)} — is that the database you meant?)")
            return 0

        print(f"{len(damaged)} stor{'y' if len(damaged) == 1 else 'ies'} "
              f"carrying a stored fallback:\n")
        for d in damaged:
            print(f"  {d['id']}  {d['category']:10} {d['created_at']:%Y-%m-%d %H:%M}  "
                  f"{d['article_count']:3} articles / {d['outlets']:2} outlets")
            print(f"      now: {d['headline'][:90]}")

        if args.dry_run:
            print(f"\ndry run — would re-synthesize {len(damaged)} "
                  f"(~${0.0022 * len(damaged):.4f}). Re-run with --apply.")
            return 0

        # Snapshot before touching anything. These rows are live in the feed;
        # a bad re-synthesis must be revertible without a database restore.
        snapshot = [dict(r) for r in await conn.fetch(
            "SELECT id, headline, summary, framings, synthesis_status FROM stories"
            " WHERE id = ANY($1::text[])", [d["id"] for d in damaged],
        )]
        os.makedirs(os.path.dirname(args.backup), exist_ok=True)
        with open(args.backup, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)
        print(f"\nbacked up {len(snapshot)} rows to {args.backup}")

        repaired = failed = skipped = 0
        for d in damaged:
            members = await members_of(conn, d["id"])
            if len(members) < 2:
                # synthesize_story returns _fallback() below 2 members without
                # making a call, so this row cannot be repaired by re-asking.
                print(f"  SKIP    {d['id']} — {len(members)} member(s), "
                      f"nothing to synthesize across")
                skipped += 1
                continue

            synthesis = await synthesize_story(members)
            if synthesis.get("_failed"):
                # Left as-is on purpose — see the module docstring. Its text is
                # wrong, but it is at least visible and re-runnable.
                print(f"  FAILED  {d['id']} — synthesis failed again, left untouched")
                failed += 1
                continue

            await conn.execute(
                """UPDATE stories
                      SET headline = $2, summary = $3, framings = $4::jsonb,
                          updated_at = NOW()
                    WHERE id = $1""",
                d["id"], synthesis["headline"], synthesis["summary"],
                json.dumps(synthesis.get("framings", [])),
            )
            repaired += 1
            print(f"  OK      {d['id']} — {len(synthesis.get('framings', []))} framings")
            print(f"      new: {synthesis['headline'][:90]}")

        print(f"\nrepaired {repaired}, failed {failed}, skipped {skipped}")
        # A re-synthesis that failed again is not a script error — it is the
        # same transient the repair exists for. Non-zero so a wrapper notices.
        return 1 if failed else 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
