"""Retarget the orphaned `type: "judge"` entity_links left by migration 016.

One-off repair. Migration 016 retired `judge_profiles` and moved the nine
Justices into `politician_profiles`, but the 87 `judge` links already written
into `articles.entity_links` still carry a type no consumer knows:
`sift/lib/entityLinks.ts` drops unknown types, so they render as nothing.

A blind `judge` -> `politician` swap would be wrong. Half these links do not
denote a person at all — the branch linker that wrote them mapped institution
mentions onto the Chief Justice, so "Supreme Court" chips to John Roberts on
20 articles and one article fans a single "SCOTUS" mention out to all nine
Justices. Converting those to `politician` would take links that currently
render as nothing and start rendering them as a false claim.

So each link is routed by what its surface form actually names:

  1. **Names the Justice** ("Justice Gorsuch", "Clarence Thomas", "Alito")
     -> `politician`, same canonical_id. It resolves now.

  2. **Names the institution** ("Supreme Court", "SCOTUS", "U.S. Supreme
     Court") -> `org` / `supreme-court-of-the-united-states`. That dossier
     exists, and `entity_aliases` ALREADY maps both surface forms to it —
     so this makes history agree with current curated policy rather than
     inventing a mapping.

  3. **Names neither** ("Chief Justice" — an office, and whoever holds it
     changes; "Heller Supreme Court decision" — a case) -> dropped. Two
     links. An alias is a claim that two names denote the same entity, and
     neither of these does.

Bucket 2 collapses duplicates: an article that linked one institution mention
to nine Justices ends up with one org link, and a converted link is dropped
if the article already carries an entry with the same (type, canonical_id).
Entries this script does not touch are preserved byte-for-byte.

Read-only until `--apply`. Writes a JSON backup of every affected row's prior
`entity_links` first, and refuses to run if that file cannot be written.

    ./.venv/bin/python3 scripts/retarget_judge_entity_links.py --dry-run
    ./.venv/bin/python3 scripts/retarget_judge_entity_links.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

# canonical_id -> surname that must appear in the surface form for the link to
# be about the person rather than the Court.
SURNAMES = {
    "SCOTUS-ROBERTS-J": "Roberts",
    "SCOTUS-THOMAS-C": "Thomas",
    "SCOTUS-ALITO-S": "Alito",
    "SCOTUS-SOTOMAYOR-S": "Sotomayor",
    "SCOTUS-KAGAN-E": "Kagan",
    "SCOTUS-GORSUCH-N": "Gorsuch",
    "SCOTUS-KAVANAUGH-B": "Kavanaugh",
    "SCOTUS-BARRETT-A": "Barrett",
    "SCOTUS-JACKSON-KB": "Jackson",
}

COURT_ORG_ID = "supreme-court-of-the-united-states"

# Surface forms that denote the institution. Matched on a normalized form so
# "US supreme court" and "U.S. Supreme Court" both land here.
INSTITUTION_FORMS = {
    "supreme court",
    "scotus",
    "us supreme court",
    "united states supreme court",
    "the supreme court",
}


def _norm(surface_form: str) -> str:
    return " ".join(surface_form.lower().replace(".", "").split())


def classify(link: dict) -> tuple[str, dict | None]:
    """Route one judge link. Returns (bucket, replacement-or-None)."""
    cid = link.get("canonical_id", "")
    surface = link.get("surface_form", "")
    surname = SURNAMES.get(cid)

    if surname and surname.lower() in surface.lower():
        return "politician", {**link, "type": "politician"}

    if _norm(surface) in INSTITUTION_FORMS:
        return "org", {
            "type": "org",
            "canonical_id": COURT_ORG_ID,
            "surface_form": surface,
        }

    return "dropped", None


def rewrite(links: list) -> tuple[list, Counter]:
    """Rewrite one article's entity_links array. Untouched entries preserved."""
    counts: Counter = Counter()
    out: list = []
    seen: set[tuple[str, str]] = set()

    # Non-judge entries first, so an existing org/politician link wins over a
    # converted one carrying the same target.
    for link in links:
        if isinstance(link, dict) and link.get("type") != "judge":
            out.append(link)
            seen.add((link.get("type", ""), link.get("canonical_id", "")))

    for link in links:
        if not isinstance(link, dict) or link.get("type") != "judge":
            continue
        bucket, replacement = classify(link)
        counts[bucket] += 1
        if replacement is None:
            continue
        key = (replacement["type"], replacement["canonical_id"])
        if key in seen:
            counts["deduped"] += 1
            continue
        seen.add(key)
        out.append(replacement)

    return out, counts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup",
        default=os.path.expanduser("~/sift-backups/judge_entity_links_backup.json"),
    )
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        from app.config import settings
        url = settings.database_url
    conn = await asyncpg.connect(
        url, **({"ssl": "require"} if "neon.tech" in url else {})
    )
    try:
        rows = await conn.fetch(
            "SELECT id, entity_links FROM articles "
            "WHERE entity_links @> '[{\"type\":\"judge\"}]' ORDER BY id"
        )
        print(f"{len(rows)} articles carry at least one judge link", file=sys.stderr)

        backup: dict[str, list] = {}
        writes: list[tuple] = []
        totals: Counter = Counter()

        for row in rows:
            links = json.loads(row["entity_links"])
            backup[row["id"]] = links
            new_links, counts = rewrite(links)
            totals.update(counts)
            if new_links != links:
                writes.append((row["id"], json.dumps(new_links)))

        print(
            f"\nlink occurrences routed:\n"
            f"  -> politician (names the Justice) : {totals['politician']}\n"
            f"  -> org / {COURT_ORG_ID} : {totals['org']}\n"
            f"  dropped (names neither)           : {totals['dropped']}\n"
            f"  collapsed as duplicates           : {totals['deduped']}\n"
            f"\narticles to update: {len(writes)}",
            file=sys.stderr,
        )

        if args.dry_run:
            print("\n--dry-run: nothing written.", file=sys.stderr)
            return 0

        os.makedirs(os.path.dirname(args.backup), exist_ok=True)
        with open(args.backup, "w", encoding="utf-8") as fh:
            json.dump(backup, fh, indent=2)
        print(f"\nbackup written -> {args.backup}", file=sys.stderr)

        async with conn.transaction():
            await conn.executemany(
                "UPDATE articles SET entity_links = $2::jsonb, updated_at = NOW() "
                "WHERE id = $1",
                writes,
            )
        print(f"articles updated: {len(writes)}", file=sys.stderr)

        remaining = await conn.fetchval(
            "SELECT count(*) FROM articles WHERE entity_links @> '[{\"type\":\"judge\"}]'"
        )
        print(
            f"\nVerified against the database:\n"
            f"  articles still carrying a judge link: {remaining} (want 0)",
            file=sys.stderr,
        )
        return 0 if remaining == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
