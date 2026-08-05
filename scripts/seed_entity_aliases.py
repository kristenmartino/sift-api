"""Seed entity_aliases from data/entity_aliases.csv.

Run from sift-api root:
    railway run ./.venv/bin/python3 scripts/seed_entity_aliases.py --dry-run
    railway run ./.venv/bin/python3 scripts/seed_entity_aliases.py

Why this table exists: the 2026-08-05 audit found entity_links on only 7.6% of
articles, and much of the miss is surface forms rather than missing dossiers —
"Pentagon" (756 articles) never chips although the DoD dossier exists.

Why it is a *curated* table and not derived: #40 removed mechanically-derived
last-name aliases because common-noun surnames (Cloud, Self, Banks, Hill)
false-matched constantly. See politician_aliases() in services/entity_linker.py.
The lesson was that derived aliases are unsafe, not that aliases are — so every
row here is hand-checked and this script refuses anything it cannot verify.

Four validations, all fatal to the row (never to the run):
  1. The target must exist in the right profile table. A dangling alias means
     a dossier was deleted or a slug changed — the D40 drift failure.
  2. The alias must survive build_search_dict: >= _MIN_CURATED_KEY_LENGTH and
     not a stopword. Otherwise the regex path silently drops it and the row is
     a lie. Curated rows use the lower floor, not _MIN_KEY_LENGTH — the 4-char
     rule exists to suppress *derived* keys, and applying it here would reject
     the three-letter outlet names ("CNN", "NPR", "Vox", the bare "BBC") that
     are among the most-mentioned entities in the corpus.
  3. The alias must not already be some *other* entity's canonical name.
     Aliasing "Congress" onto an org is fine; aliasing "Susan Collins" onto
     someone else is not.
  4. The alias must be unambiguous within the catalog — if it appears as a
     whole word in another profile name, that collision must be one the
     linker can actually resolve. This is the check that keeps "Kennedy",
     "Miller" and "Collins" out. See `blocking_conflicts` for what counts
     as resolvable now that `link_text` does longest-match-wins.

Idempotent UPSERT on the alias PK. --prune removes DB rows absent from the CSV,
so unlike the other seeders this one can be the source of truth.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings
from services.entity_linker import _MIN_CURATED_KEY_LENGTH, _STOPWORDS

VALID_TYPES = {"politician", "org", "bill", "outlet"}

# entity_type -> (table, pk column)
_TARGET = {
    "politician": ("politician_profiles", "bioguide_id"),
    "org": ("org_profiles", "slug"),
    "bill": ("bill_profiles", "bill_id"),
    "outlet": ("outlet_profiles", "slug"),
}

# Function words that, appearing before the alias inside a longer profile
# name, mark the alias as a prepositional modifier rather than the name's
# head: "Library **of** Congress" is a library, not a congress. Without
# this, any name ending in the alias would look head-shared.
_HEAD_INVERTING = frozenset({"of", "for", "in", "on", "at", "and", "against"})


def blocking_conflicts(
    alias: str,
    etype: str,
    cid: str,
    names: list[tuple[str, str, str]],
) -> list[tuple[str, str]]:
    """Profiles that make `alias` genuinely ambiguous, ignoring the ones
    `link_text`'s longest-match-wins already resolves.

    `names` is [(entity_type, canonical_id, lowercased_name), ...] for the
    whole catalog; the alias's own target is skipped.

    Every profile whose name contains the alias as a whole word is a
    candidate collision. A collision is **resolvable** — and therefore not
    returned — when all three hold:

    1. The colliding profile is not a politician. People are routinely
       referred to by bare surname in news copy ("Kennedy said"), so the
       full name is not reliably present for the longer key to win on.
       This is what keeps "Kennedy", "Miller" and "Collins" rejected.
    2. The alias is a strict sub-span of that name, so there *is* a longer
       key to win.
    3. The alias is not the name's head — i.e. the name does not end with
       it, or it does but sits behind a preposition. "Congress" is the
       head of nothing in "Library of Congress" (a library), and "Postal
       Service" is a modifier in "Postal Service Reform Act" (an act), so
       both resolve. "Times" *is* the head of "Los Angeles Times", so an
       alias "times" stays rejected — a bare "the Times" has no longer
       span to lose to.
    """
    pat = re.compile(rf"\b{re.escape(alias)}\b")
    blocking: set[tuple[str, str]] = set()
    for t, i, name in names:
        if (t, i) == (etype, cid):
            continue
        match = pat.search(name)
        if match is None:
            continue
        if t == "politician":
            blocking.add((t, i))  # rule 1
            continue
        if len(name) <= len(alias):
            blocking.add((t, i))  # rule 2 — nothing longer to win the span
            continue
        if match.end() == len(name):  # rule 3 — alias is the trailing token(s)
            before = name[: match.start()].split()
            if not any(tok in _HEAD_INVERTING for tok in before):
                blocking.add((t, i))
    return sorted(blocking)


async def main(csv_path: str, dry_run: bool, prune: bool) -> int:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{csv_path} has no rows.")
        return 1

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)

    try:
        # Full catalog, for the existence and ambiguity checks.
        catalog = await pool.fetch("""
            SELECT 'politician' AS t, bioguide_id AS id, name FROM politician_profiles
            UNION ALL SELECT 'org', slug, name FROM org_profiles
            UNION ALL SELECT 'bill', bill_id, COALESCE(short_title, title) FROM bill_profiles
            UNION ALL SELECT 'outlet', slug, name FROM outlet_profiles
        """)
        ids_by_type: dict[str, set[str]] = {t: set() for t in VALID_TYPES}
        names: list[tuple[str, str, str]] = []
        for r in catalog:
            ids_by_type[r["t"]].add(r["id"])
            if r["name"]:
                names.append((r["t"], r["id"], r["name"].lower()))
        canonical_names = {n for _, _, n in names}

        accepted: list[tuple[str, str, str, str | None]] = []
        rejected: list[tuple[str, str]] = []

        for row in rows:
            alias = (row.get("alias") or "").strip().lower()
            etype = (row.get("entity_type") or "").strip()
            cid = (row.get("canonical_id") or "").strip()
            notes = (row.get("notes") or "").strip() or None

            if not alias or not etype or not cid:
                rejected.append((alias or "(blank)", "missing a required field"))
                continue
            if etype not in VALID_TYPES:
                rejected.append((alias, f"unknown entity_type {etype!r}"))
                continue
            # 1. Target must exist.
            if cid not in ids_by_type[etype]:
                table = _TARGET[etype][0]
                rejected.append((alias, f"no {table} row with id {cid!r}"))
                continue
            # 2. Must survive build_search_dict. Every row here is by
            # definition curated, so it gets the same lower floor
            # build_search_dict grants curated keys — otherwise this check
            # would reject exactly the rows that floor was relaxed for
            # ("bbc", "cnn"), and the two would silently disagree.
            if len(alias) < _MIN_CURATED_KEY_LENGTH:
                rejected.append((
                    alias,
                    f"shorter than _MIN_CURATED_KEY_LENGTH ({_MIN_CURATED_KEY_LENGTH})",
                ))
                continue
            if alias in _STOPWORDS:
                rejected.append((alias, "is a linker stopword"))
                continue
            # 3. Must not be another entity's canonical name.
            if alias in canonical_names:
                owner = next(
                    ((t, i) for t, i, n in names if n == alias and (t, i) != (etype, cid)),
                    None,
                )
                if owner is not None:
                    rejected.append((alias, f"is the canonical name of {owner[0]} {owner[1]!r}"))
                    continue
            # 4. Any remaining ambiguity must be one longest-match-wins fixes.
            hits = blocking_conflicts(alias, etype, cid, names)
            if hits:
                sample = ", ".join(f"{t}:{i}" for t, i in hits[:3])
                rejected.append((
                    alias,
                    f"ambiguous — also matches {len(hits)} other profile(s): {sample}",
                ))
                continue

            accepted.append((alias, etype, cid, notes))

        existing = {
            r["alias"] for r in await pool.fetch("SELECT alias FROM entity_aliases")
        }
        accepted_aliases = {a for a, _, _, _ in accepted}
        stale = sorted(existing - accepted_aliases)

        print(f"CSV rows:        {len(rows)}")
        print(f"  accepted:      {len(accepted)}")
        print(f"  rejected:      {len(rejected)}")
        print(f"Already in DB:   {len(existing)}")
        print(f"  not in CSV:    {len(stale)}"
              f"{' (will DELETE)' if prune and not dry_run else ' (left alone; --prune to remove)'}")
        print()

        if rejected:
            print("Rejected rows — each is dropped, the run continues:")
            for alias, why in rejected:
                print(f"  {alias:<28} {why}")
            print()

        if dry_run:
            print("Dry run — nothing written.")
            for alias, etype, cid, _ in accepted[:10]:
                print(f"  would upsert  {alias:<28} -> {etype}:{cid}")
            if len(accepted) > 10:
                print(f"  … and {len(accepted) - 10} more")
            return 0

        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO entity_aliases (alias, entity_type, canonical_id, notes)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (alias) DO UPDATE
                  SET entity_type  = EXCLUDED.entity_type,
                      canonical_id = EXCLUDED.canonical_id,
                      notes        = EXCLUDED.notes
                """,
                accepted,
            )
            if prune and stale:
                await conn.execute(
                    "DELETE FROM entity_aliases WHERE alias = ANY($1::text[])", stale
                )

        print(f"Upserted {len(accepted)} aliases.")
        if prune and stale:
            print(f"Deleted {len(stale)} stale aliases: {', '.join(stale)}")
        print()
        print("Next: the linker picks these up on its next run (build_catalog attaches")
        print("them to CatalogRow.aliases). Re-run scripts/backfill_entity_links.py to")
        print("apply them to articles already stored.")
        return 0

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed curated entity aliases.")
    parser.add_argument("--input", default="data/entity_aliases.csv")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and report without writing.")
    parser.add_argument("--prune", action="store_true",
                        help="DELETE aliases present in the DB but absent from the CSV.")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.input, args.dry_run, args.prune)))
