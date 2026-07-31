"""Clear the model's stored apologies out of article summaries (sift-api#118).

`services/quality_gate.py:gate_summary` stops NEW refusals at write time. It
does nothing for rows already in the table — `ON CONFLICT DO UPDATE` in the
pipeline never regenerates a stored summary, and `deduplicator` drops known
source_urls before summarization, so nothing reprocesses them on its own. This
script is that something, mirroring scripts/regate_existing.py for
`why_it_matters`.

It runs the same deterministic gate over stored rows and splits them two ways:

  TRIMMED   the summary contained an apology sentence AND real reporting. The
            apology goes, the reporting stays, read_time is recalculated. The
            embedding is deliberately left alone: it is now marginally stale
            (built from title + summary including the removed sentence), and a
            slightly stale vector beats a NULL one, which would drop the
            article out of vector search and story threading entirely.

  BLANKED   nothing survived the gate. summary becomes '', which every feed
            query in sift/lib/db.ts filters out — so the article stops being
            served rather than showing an apology. Everything derived FROM
            that apology is cleared too (why_it_matters, importance_score,
            context_primer, reading_levels, entities, entity_links, story_id,
            embedding), on the same reasoning as
            scripts/repair_misaligned_summaries.py: a row whose summary was
            garbage has garbage derived from it, and leaving those behind
            makes it look repaired while it still behaves wrong.

Idempotent — a second run finds nothing, because the gate is inert on text it
has already cleaned.

DRY RUN by default. Pass --apply to write.

Examples:
  ./.venv/bin/python3 scripts/regate_summaries.py                 # dry run, 30 days
  ./.venv/bin/python3 scripts/regate_summaries.py --days 90
  ./.venv/bin/python3 scripts/regate_summaries.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services.quality_gate import find_refusal, gate_summary  # noqa: E402


async def _connect() -> asyncpg.Pool:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    return await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)


def _read_time(summary: str) -> int:
    """Same formula as workflows/pipeline_workflow.py:store_node."""
    return max(1, len(summary.split()) // 200 + 1) if summary else 1


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="lookback window (default 30)")
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    parser.add_argument("--limit", type=int, default=100_000, help="max rows to scan")
    args = parser.parse_args()

    pool = await _connect()
    try:
        rows = await pool.fetch(
            """
            SELECT source_url, source_name, title, summary
              FROM articles
             WHERE summary IS NOT NULL AND summary <> ''
               AND from_search = false
               AND created_at > NOW() - make_interval(days => $1)
             ORDER BY created_at DESC
             LIMIT $2
            """,
            args.days, args.limit,
        )
        print(f"scanned {len(rows)} stored summaries over the last {args.days} day(s)")

        trimmed: list[tuple[str, str, str]] = []  # (url, before, after)
        blanked: list[tuple[str, str, str]] = []  # (url, title, before)
        for r in rows:
            gated = gate_summary(r["summary"])
            if gated == r["summary"]:
                continue
            if gated:
                trimmed.append((r["source_url"], r["summary"], gated))
            else:
                blanked.append((r["source_url"], r["title"], r["summary"]))

        print(f"  {len(blanked)} to blank, {len(trimmed)} to trim\n")
        for _url, before, after in trimmed[:5]:
            print(f"  TRIM   {find_refusal(before)!r} removed")
            print(f"         after: {after[:110]}")
        for _url, title, before in blanked[:8]:
            print(f"  BLANK  {title[:58]}")
            print(f"         was: {before[:100]}")

        if not args.apply:
            print(f"\nDRY RUN — nothing written. {len(blanked) + len(trimmed)} rows would change.")
            return

        for url, _before, after in trimmed:
            await pool.execute(
                "UPDATE articles SET summary = $1, read_time = $2, updated_at = NOW() "
                "WHERE source_url = $3",
                after, _read_time(after), url,
            )

        if blanked:
            # Everything below was generated FROM the apology, so it is cleared
            # for the backfill scripts and the next threading run to rebuild.
            await pool.execute(
                """
                UPDATE articles
                   SET summary = '', read_time = 1,
                       why_it_matters = NULL, importance_score = NULL,
                       context_primer = NULL, reading_levels = NULL,
                       entities = '[]'::jsonb, entity_links = '[]'::jsonb,
                       story_id = NULL, embedding = NULL,
                       updated_at = NOW()
                 WHERE source_url = ANY($1::text[])
                """,
                [u for u, _, _ in blanked],
            )

        print(f"\nAPPLIED: trimmed {len(trimmed)}, blanked {len(blanked)}.")
        if blanked:
            print("Derived fields cleared on the blanked rows. Regenerate with: "
                  "backfill_context.py, backfill_primers.py, backfill_entity_links.py")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
