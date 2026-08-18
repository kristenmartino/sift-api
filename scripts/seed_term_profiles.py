"""Seed term_profiles from data/term_profiles.csv.

Run from sift-api root:
    railway run ./.venv/bin/python3 scripts/seed_term_profiles.py --dry-run
    railway run ./.venv/bin/python3 scripts/seed_term_profiles.py

These back `/term/<slug>` — a definition plus the corpus coverage of that term.

**Why these are hand-written when the primers already have ~11,900.** Every
`context_primer.terms[].source` in the corpus is null. A primer definition is
an inline reading aid attached to an article; a term page is a standalone claim
about what a legal term means, on Sift's own authority. Publishing the former
as the latter is what migrations 013 and 015 each had to undo. So the rule is
the same one the rest of the schema uses: no claim without the record behind
it.

Five validations, each fatal to the row and never to the run:
  1. slug, term, definition and definition_source must all be present.
  2. definition_source must be an https URL. A citation you cannot click is
     not a citation.
  3. `aliases` must parse as a JSON array of non-empty strings. These become
     whole-word match keys in the coverage query, so a malformed one silently
     widens or narrows what the page claims to cover.
  4. No two rows may claim the same slug or the same alias — an alias that
     resolves to two terms would put the same article under both.
  5. definition_checked must be YYYY-MM-DD. Parsed here rather than passed to
     Postgres as text: asyncpg binds a `date` column by type, so a bad string
     is a DataError that aborts the whole executemany, taking the good rows
     with it. Parsing turns that into one dropped row.

Idempotent UPSERT on slug. `--prune` deletes rows absent from the CSV; unlike
the politician seeder that is safe here, because nothing references a term row
(no entity_links, no foreign keys) and the definition is reproducible from the
CSV that is its source of truth.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from datetime import date

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "term_profiles.csv",
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HTTPS_RE = re.compile(r"^https://", re.I)


def _parse_aliases(raw: str | None) -> list[str] | None:
    """Return the alias list, or None if the cell is malformed."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    out: list[str] = []
    for a in parsed:
        if not isinstance(a, str) or not a.strip():
            return None
        out.append(a.strip())
    return out


def validate(rows: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    accepted: list[dict] = []
    rejected: list[tuple[str, str]] = []
    seen_slugs: set[str] = set()
    seen_aliases: dict[str, str] = {}

    for raw in rows:
        slug = (raw.get("slug") or "").strip().lower()
        term = (raw.get("term") or "").strip()
        definition = (raw.get("definition") or "").strip()
        source = (raw.get("definition_source") or "").strip()
        label = slug or term or "(blank)"

        if not slug or not term or not definition or not source:
            rejected.append((label, "missing slug, term, definition or definition_source"))
            continue
        if not _SLUG_RE.match(slug):
            rejected.append((label, f"slug {slug!r} is not url-safe kebab-case"))
            continue
        if not _HTTPS_RE.match(source):
            rejected.append((label, f"definition_source {source!r} is not an https URL"))
            continue
        if slug in seen_slugs:
            rejected.append((label, "duplicate slug in the CSV"))
            continue

        aliases = _parse_aliases(raw.get("aliases"))
        if aliases is None:
            rejected.append((label, "aliases is not a JSON array of non-empty strings"))
            continue

        checked_raw = (raw.get("definition_checked") or "").strip()
        try:
            checked = date.fromisoformat(checked_raw) if checked_raw else None
        except ValueError:
            rejected.append((label, f"definition_checked {checked_raw!r} is not YYYY-MM-DD"))
            continue

        clash = next(
            (a for a in aliases if a.lower() in seen_aliases
             and seen_aliases[a.lower()] != slug),
            None,
        )
        if clash:
            rejected.append((label, f"alias {clash!r} already claimed by {seen_aliases[clash.lower()]!r}"))
            continue

        seen_slugs.add(slug)
        for a in aliases:
            seen_aliases[a.lower()] = slug

        accepted.append({
            "slug": slug,
            "term": term,
            "definition": definition,
            "definition_source": source,
            "definition_checked": checked,
            "aliases": json.dumps(aliases, separators=(",", ":")),
            "category": (raw.get("category") or "").strip() or None,
            "notes": (raw.get("notes") or "").strip() or None,
        })
    return accepted, rejected


async def main(csv_path: str, dry_run: bool, prune: bool) -> int:
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found.", file=sys.stderr)
        return 1
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    accepted, rejected = validate(rows)
    print(f"CSV rows:   {len(rows)}")
    print(f"  accepted: {len(accepted)}")
    print(f"  rejected: {len(rejected)}")
    if rejected:
        print("\nRejected — dropped, run continues:")
        for label, why in rejected:
            print(f"  {label:<34} {why}")
    if not accepted:
        print("\nNothing to write.")
        return 1

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl)
    try:
        existing = {r["slug"] for r in await pool.fetch("SELECT slug FROM term_profiles")}
        stale = sorted(existing - {a["slug"] for a in accepted})
        print(f"\nAlready in DB: {len(existing)}")
        print(f"  not in CSV:  {len(stale)}"
              f"{' (will DELETE)' if prune and not dry_run else ' (--prune to remove)'}")

        if dry_run:
            print("\n--dry-run set; no DB writes.")
            for a in accepted:
                print(f"  would upsert  /term/{a['slug']}")
            return 0

        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO term_profiles
                  (slug, term, definition, definition_source, definition_checked,
                   aliases, category, notes, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                  term               = EXCLUDED.term,
                  definition         = EXCLUDED.definition,
                  definition_source  = EXCLUDED.definition_source,
                  definition_checked = EXCLUDED.definition_checked,
                  aliases            = EXCLUDED.aliases,
                  category           = EXCLUDED.category,
                  notes              = EXCLUDED.notes,
                  updated_at         = NOW()
                """,
                [(a["slug"], a["term"], a["definition"], a["definition_source"],
                  a["definition_checked"], a["aliases"], a["category"], a["notes"])
                 for a in accepted],
            )
            if prune and stale:
                await conn.execute(
                    "DELETE FROM term_profiles WHERE slug = ANY($1::text[])", stale
                )

        print(f"\nUpserted {len(accepted)} terms.")
        if prune and stale:
            print(f"Deleted {len(stale)}: {', '.join(stale)}")

        # Seeding a term does not publish it. /glossary and the publish floor
        # read the counts migration 034 stores, and a row this script just
        # inserted has article_count NULL until refresh_term_coverage.py
        # measures it — which the floor treats as zero.
        #
        # That is the fail-closed behaviour working as designed, and it is
        # indistinguishable from a bug unless something says so. seed_all.sh
        # runs the refresh for you; anyone invoking this script directly gets
        # told here, by name, with the command.
        unmeasured = [
            r["slug"] for r in await pool.fetch(
                """
                SELECT slug FROM term_profiles
                 WHERE definition IS NOT NULL AND definition_source IS NOT NULL
                   AND (article_count IS NULL OR coverage_computed_at IS NULL)
                 ORDER BY slug
                """
            )
        ]
        if unmeasured:
            print(
                f"\n!! {len(unmeasured)} term(s) have no measured coverage and "
                "will NOT publish:"
            )
            for slug in unmeasured:
                print(f"     /term/{slug}")
            print("\n   Fix (or just run scripts/seed_all.sh, which does it):")
            print("     railway run ./.venv/bin/python3 scripts/refresh_term_coverage.py")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed curated term definitions.")
    ap.add_argument("--input", default=CSV_PATH)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.input, args.dry_run, args.prune)))
