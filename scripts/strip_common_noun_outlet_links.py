"""Remove stored outlet chips that fired on a common noun, not on the outlet.

One-off repair, deterministic and free. `_REGEX_INELIGIBLE_NAMES` in
services/entity_linker.py stops these being *created*; it cannot remove the
ones already in `articles.entity_links`. Neither backfill can either:
`backfill_entity_links.py --mode regex` is additive by design and can only add
chips, and `--mode llm` is a destructive overwrite driven by a per-article
model call — the write path that emptied 218 rows on a timeout wave (#136).
A removal this narrow should not need a model, and does not.

WHAT IT REMOVES
---------------
Only the bare blocked word, and only in a casing the outlet itself does not
use. `outlet_profiles.name` for `stat-news` is "STAT", so:

    surface_form 'stat' / 'Stat'   -> the common noun. Dropped.
    surface_form 'STAT'            -> the outlet's own styling. Kept.
    surface_form 'STAT News'       -> names the outlet. Kept.
    every other link on the row    -> untouched, byte for byte.

That casing split is not a guess. Measured against prod 2026-08-06 over all
873 stored `stat-news` chips:

    'STAT'      751   every one on STAT News's own article
    'STAT News'  11   every one on STAT News's own article
    'stat'       61   every one on somebody else's article
    'Stat'       50   every one on somebody else's article

and the 111 lowercase/title-case ones are the common noun without exception —
"Red Sox's Most Absurd Stat", "this Walker Kessler stat", "add to your cart
stat". Carriers: Sports Illustrated 58, Yahoo Sports 29, Fox News 7, New York
Post 5, CBS Sports 3, The Athletic 3, plus singletons. The corpus holds zero
third-party mentions of STAT in any casing, so the casing carve-out costs
nothing today and is kept because it is the honest rule: it says "this surface
form is the common noun", which is the claim actually being made.

NOT THE SELF-REFERENCE FIX, deliberately. The 762 chips on STAT News's own
articles are a separate problem with a separate cause (an outlet naming itself
tells a reader nothing `source_name` did not), handled by the
source_name_aliases work. Keying this script on casing rather than on
`source_name` keeps the two orthogonal, so either can land first and neither
has to know about the other. `--report-sources` prints the split so you can
confirm the two sets stay disjoint.

Read-only until `--apply`. Writes a JSON backup of every affected row's prior
`entity_links` first, and refuses to run if that file cannot be written.

    ./.venv/bin/python3 scripts/strip_common_noun_outlet_links.py --dry-run
    ./.venv/bin/python3 scripts/strip_common_noun_outlet_links.py --apply

Idempotent: a second run finds nothing.
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

from services.entity_linker import _REGEX_INELIGIBLE_NAMES  # noqa: E402

# outlet canonical_id -> (blocked bare name, the styling the outlet uses for it)
#
# One entry per name that is BOTH in `_REGEX_INELIGIBLE_NAMES` and already
# stored on rows. The other ten blocked names were cleaned when they were
# added (#141), so they are not listed — adding one here without measuring its
# casing split first would delete real links.
#
# Not extended to `variety` / `the athletic` / `wired` / `the hill`, whose
# stored chips are also substantially false (measured 2026-08-06): those names
# are not blocked, they have real correctly-cased mentions, and their fix is a
# matcher change rather than a cleanup. Tracked as sift-api#151.
COMMON_NOUN_OUTLETS: dict[str, tuple[str, str]] = {
    "stat-news": ("stat", "STAT"),
}


def is_common_noun_link(link: dict) -> bool:
    """True when this link is the blocked bare word in a non-outlet casing."""
    if not isinstance(link, dict) or link.get("type") != "outlet":
        return False
    rule = COMMON_NOUN_OUTLETS.get(link.get("canonical_id", ""))
    if rule is None:
        return False
    blocked, styled = rule
    surface = link.get("surface_form") or ""
    return surface.strip().lower() == blocked and surface.strip() != styled


def rewrite(links: list) -> tuple[list, Counter]:
    """Drop the common-noun links from one article. Everything else preserved."""
    counts: Counter = Counter()
    out: list = []
    for link in links:
        if is_common_noun_link(link):
            counts[f"dropped:{link['canonical_id']}"] += 1
            continue
        out.append(link)
    return out, counts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup",
        default=os.path.expanduser("~/sift-backups/common_noun_outlet_links_backup.json"),
    )
    parser.add_argument(
        "--report-sources", action="store_true",
        help="print the carrier split, to confirm this and the self-reference "
             "cleanup are touching disjoint sets",
    )
    args = parser.parse_args()

    # A name here that is not actually blocked would mean this script is
    # deleting links the linker will simply recreate on the next regex pass.
    for cid, (blocked, _styled) in COMMON_NOUN_OUTLETS.items():
        if blocked not in _REGEX_INELIGIBLE_NAMES:
            print(
                f"refusing to run: {blocked!r} ({cid}) is not in "
                "_REGEX_INELIGIBLE_NAMES, so the linker would recreate every "
                "link this removes.",
                file=sys.stderr,
            )
            return 2

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        from app.config import settings
        url = settings.database_url
    conn = await asyncpg.connect(
        url, **({"ssl": "require"} if "neon.tech" in url else {})
    )
    try:
        targets = list(COMMON_NOUN_OUTLETS)
        rows = await conn.fetch(
            """
            SELECT a.id, a.source_name, a.entity_links
            FROM articles a
            WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements(a.entity_links) l
                WHERE l->>'type' = 'outlet'
                  AND l->>'canonical_id' = ANY($1::text[])
            )
            ORDER BY a.id
            """,
            targets,
        )
        print(
            f"{len(rows)} articles carry a chip for: {', '.join(targets)}",
            file=sys.stderr,
        )

        backup: dict[str, list] = {}
        writes: list[tuple] = []
        totals: Counter = Counter()
        dropped_sources: Counter = Counter()
        kept_sources: Counter = Counter()

        for row in rows:
            links = json.loads(row["entity_links"])
            new_links, counts = rewrite(links)
            totals.update(counts)
            source = row["source_name"] or "?"
            if counts:
                dropped_sources[source] += sum(counts.values())
            else:
                kept_sources[source] += 1
            if new_links != links:
                backup[row["id"]] = links
                writes.append((row["id"], json.dumps(new_links)))

        print("\nlinks dropped:", file=sys.stderr)
        for key, n in sorted(totals.items()):
            print(f"  {key}: {n}", file=sys.stderr)
        print(f"articles to update: {len(writes)}", file=sys.stderr)

        if args.report_sources:
            print("\ncarriers of the DROPPED links:", file=sys.stderr)
            for src, n in dropped_sources.most_common():
                print(f"  {n:5d}  {src}", file=sys.stderr)
            print("\ncarriers of the KEPT links:", file=sys.stderr)
            for src, n in kept_sources.most_common(10):
                print(f"  {n:5d}  {src}", file=sys.stderr)

        if args.dry_run:
            print("\n--dry-run: nothing written.", file=sys.stderr)
            return 0

        if not writes:
            print("\nnothing to do.", file=sys.stderr)
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

        # Verify against the database rather than against our own arithmetic.
        remaining = 0
        for cid, (blocked, styled) in COMMON_NOUN_OUTLETS.items():
            left = await conn.fetch(
                """
                SELECT l->>'surface_form' AS sf
                FROM articles a, jsonb_array_elements(a.entity_links) l
                WHERE l->>'type' = 'outlet' AND l->>'canonical_id' = $1
                """,
                cid,
            )
            bad = [
                r["sf"] for r in left
                if (r["sf"] or "").strip().lower() == blocked
                and (r["sf"] or "").strip() != styled
            ]
            remaining += len(bad)
            print(
                f"\nVerified against the database — {cid}:\n"
                f"  chips remaining        : {len(left)}\n"
                f"  common-noun casings    : {len(bad)} (want 0)",
                file=sys.stderr,
            )
        return 0 if remaining == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
