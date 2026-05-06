"""Audit articles.source_name values against outlet_profiles + suggest aliases.

Run from sift-api root:
    railway run ./.venv/bin/python3 scripts/audit_source_aliases.py
    railway run ./.venv/bin/python3 scripts/audit_source_aliases.py --output suggestions.csv

What it does:
1. Pulls distinct `articles.source_name` values from prod, with article counts.
2. Looks up which `source_name` values are already mapped in `source_name_aliases`.
3. For each unmapped source_name, attempts a fuzzy match against
   `outlet_profiles.name` using case-insensitive substring matching.
4. Outputs a CSV ready for human review:

       source_name | article_count | suggested_outlet_slug | confidence

   Confidence values:
     - "exact"        — case-insensitive equality with an outlet_profiles.name
     - "substring"    — case-insensitive substring match (one direction or other)
     - "none"         — no candidate; user decides whether to add the outlet to
                        outlet_profiles or leave the source_name unmapped.

5. Reviewer edits the CSV (corrects suggestions, fills "none" rows or leaves
   empty), then runs scripts/seed_source_aliases.py against the same CSV.

Designed to be re-run periodically: as new outlets surface in the feed
(through new RSS feeds or messy source_name variations), this catches them.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings


def _normalize(s: str) -> str:
    return s.strip().lower()


def _suggest_match(
    source_name: str,
    outlets: list[tuple[str, str]],  # [(slug, name)]
) -> tuple[str | None, str]:
    """Return (suggested_slug, confidence)."""
    sn = _normalize(source_name)
    if not sn:
        return None, "none"

    # Exact match (case-insensitive)
    for slug, name in outlets:
        if _normalize(name) == sn:
            return slug, "exact"

    # Substring match (either direction). Score by length similarity to
    # break ties — longer matches in either direction are stronger signals.
    best: tuple[str, str] | None = None
    best_score = -1
    for slug, name in outlets:
        on = _normalize(name)
        if on in sn or sn in on:
            score = min(len(sn), len(on))
            if score > best_score:
                best = (slug, "substring")
                best_score = score

    if best is not None:
        return best
    return None, "none"


async def main(output_path: str) -> None:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    try:
        # 1. Distinct source_names + counts
        rows = await pool.fetch(
            """
            SELECT source_name, COUNT(*)::int AS article_count
            FROM articles
            WHERE source_name IS NOT NULL
              AND source_name <> ''
              AND from_search = false
            GROUP BY source_name
            ORDER BY article_count DESC
            """
        )

        # 2. Already-mapped source_names
        mapped_rows = await pool.fetch(
            "SELECT raw_source_name FROM source_name_aliases"
        )
        already_mapped = {r["raw_source_name"] for r in mapped_rows}

        # 3. outlet_profiles for matching
        outlet_rows = await pool.fetch(
            "SELECT slug, name FROM outlet_profiles ORDER BY name"
        )
        outlets = [(r["slug"], r["name"]) for r in outlet_rows]

        if not outlets:
            print(
                "WARN: outlet_profiles is empty — run "
                "scripts/seed_outlet_profiles.py first. "
                "Suggestions will all be 'none'."
            )

        suggestions: list[dict] = []
        unmapped_total_articles = 0
        already_mapped_count = 0
        for r in rows:
            source_name = r["source_name"]
            article_count = r["article_count"]
            if source_name in already_mapped:
                already_mapped_count += 1
                continue
            unmapped_total_articles += article_count
            slug, conf = _suggest_match(source_name, outlets)
            suggestions.append({
                "source_name": source_name,
                "article_count": article_count,
                "suggested_outlet_slug": slug or "",
                "confidence": conf,
            })

    finally:
        await pool.close()

    print(f"Distinct source_name values:       {len(rows)}")
    print(f"  already mapped:                  {already_mapped_count}")
    print(f"  unmapped (suggested below):      {len(suggestions)}")
    print(f"  total articles unmapped:         {unmapped_total_articles}")
    print()

    if not suggestions:
        print("Nothing to suggest. Every source_name is already in source_name_aliases.")
        return

    # Write output CSV
    fieldnames = ["source_name", "article_count", "suggested_outlet_slug", "confidence"]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in suggestions:
            writer.writerow(row)

    # Stats by confidence
    by_conf: dict[str, int] = {}
    for s in suggestions:
        by_conf[s["confidence"]] = by_conf.get(s["confidence"], 0) + 1

    print(f"Wrote {len(suggestions)} suggestions to {output_path}")
    print("Confidence breakdown:")
    for conf in ("exact", "substring", "none"):
        if conf in by_conf:
            print(f"  {conf:10s}: {by_conf[conf]}")
    print()
    print("Next steps:")
    print(f"  1. Review {output_path}; correct or fill 'suggested_outlet_slug' as needed.")
    print("  2. Drop rows you want to leave unmapped (those articles render without provenance).")
    print("  3. Run: railway run ./.venv/bin/python3 scripts/seed_source_aliases.py "
          f"--input {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit + suggest source_name → outlet_slug aliases.")
    parser.add_argument(
        "--output", default="data/source_alias_suggestions.csv",
        help="Path to write the suggestions CSV (default: data/source_alias_suggestions.csv).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.output))
