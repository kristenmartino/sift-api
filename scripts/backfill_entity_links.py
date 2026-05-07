"""Backfill articles.entity_links — re-runs the linker over already-linked rows.

Originally written as a one-shot after PR #40 dropped last-name-only
aliases (50 stored articles needed re-linking; 46 cleared to []).
Updated for Phase 3.G.2 (PR adding the LLM linker) — now exercises the
same `link_articles` entry point the pipeline node uses, so the
LLM path with prompt caching + regex fallback all run for free.

Articles that have always been empty are left untouched (the periodic
pipeline will populate them with the new policy as it processes new
content).

Idempotent. Safe to re-run; only writes a row when the new value
differs from the existing one.

Usage (from sift-api root):

    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py
    railway run ./.venv/bin/python3 scripts/backfill_entity_links.py --dry-run

Cost note: with the LLM linker active, each re-linked article costs
~$0.001 (prompt caching amortizes the catalog block). Current prod
state has ~5 affected articles → trivially cheap.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# scripts/ sits next to services/ — make the latter importable when run
# from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from services.entity_linker import build_catalog  # noqa: E402
from services.entity_linker_llm import link_articles_llm  # noqa: E402


async def main(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    ssl = "require" if "neon.tech" in db_url else False
    conn = await asyncpg.connect(db_url, ssl=ssl)
    try:
        # Catalog — same four queries the pipeline node uses.
        outlets = [dict(r) for r in await conn.fetch(
            "SELECT slug, name FROM outlet_profiles"
        )]
        politicians = [dict(r) for r in await conn.fetch(
            "SELECT bioguide_id, name FROM politician_profiles"
        )]
        orgs = [dict(r) for r in await conn.fetch(
            "SELECT slug, name FROM org_profiles"
        )]
        bills = [dict(r) for r in await conn.fetch(
            "SELECT bill_id, title, short_title FROM bill_profiles"
        )]
        catalog = build_catalog(outlets, politicians, orgs, bills)
        print(
            f"Catalog: {len(outlets)} outlets, {len(politicians)} politicians, "
            f"{len(orgs)} orgs, {len(bills)} bills → {len(catalog)} entries"
        )

        # Pull every article with a non-empty entity_links column. We
        # leave the (much larger) set of always-empty rows alone — the
        # pipeline will populate those for new articles with the corrected
        # policy.
        rows = await conn.fetch(
            """
            SELECT id, title, summary, source_url, entity_links::text AS el
            FROM articles
            WHERE entity_links IS NOT NULL
              AND entity_links::text != '[]'
            ORDER BY created_at DESC
            """
        )
        print(f"Articles with non-empty entity_links: {len(rows)}")
        if not rows:
            return

        # Run the LLM linker over all of them at once — concurrency-limited
        # internally; prompt-cache amortizes the catalog tokens across calls.
        articles = [
            {"source_url": r["source_url"], "title": r["title"] or "",
             "summary": r["summary"] or ""}
            for r in rows
        ]
        link_map = await link_articles_llm(articles, catalog)  # type: ignore[arg-type]

        # Match new links back to article ids via source_url and update.
        url_to_id = {r["source_url"]: r["id"] for r in rows}
        url_to_old_json = {r["source_url"]: r["el"] for r in rows}
        updated = 0
        no_change = 0
        cleared = 0
        for url, new_links in link_map.items():
            new_json = json.dumps(new_links, separators=(",", ":"))
            old_json = url_to_old_json.get(url, "[]")
            aid = url_to_id.get(url)
            if not aid:
                continue
            if old_json == new_json:
                no_change += 1
                continue
            if not new_links:
                cleared += 1
            updated += 1
            if not dry_run:
                await conn.execute(
                    "UPDATE articles SET entity_links = $1::jsonb WHERE id = $2",
                    new_json, aid,
                )

        print()
        print(f"  updated:     {updated}")
        print(f"    of which cleared to []: {cleared}")
        print(f"  unchanged:   {no_change}")
        if dry_run:
            print()
            print("--dry-run set; no DB writes.")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-run the entity linker over articles with stored "
                    "entity_links and write the corrected list back.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute the diff but don't write to the DB.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
