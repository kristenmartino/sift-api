"""One-off cleanup for issue #91 — remove duplicate + excluded outlet profiles.

Surgical, idempotent, transactional. DRY-RUN by default.

    # inspect (read-only):
    railway run ./.venv/bin/python3 scripts/dedupe_outlet_profiles.py
    # apply (writes, inside a transaction):
    railway run ./.venv/bin/python3 scripts/dedupe_outlet_profiles.py --apply

Five rows drifted into prod `outlet_profiles` that are NOT in the seed CSV
(data/outlet_profiles.csv); the seeder is upsert-only so it never pruned them:

  - bbc            → duplicate of canonical `bbc-news`  (refs REPOINTED)
  - bloomberg-news → duplicate of canonical `bloomberg` (refs REPOINTED)
  - yahoo-news     → aggregator, excluded per /methodology (refs dropped)
  - yahoo-finance  → "                                     (refs dropped)
  - yahoo-sports   → "                                     (refs dropped)

References handled before the rows are deleted:
  - source_name_aliases.outlet_slug (ON DELETE CASCADE): dup aliases are
    REPOINTED to the canonical slug first (else the cascade would drop the
    "BBC"/"Bloomberg" → outlet mapping); Yahoo aliases cascade away on delete.
  - articles.entity_links (JSONB {type,canonical_id,surface_form}): outlet
    dossier refs to a dup are REPOINTED to the canonical; refs to Yahoo are
    removed (no dossier to point at). Otherwise /outlet/<slug> links dangle.

Does NOT touch the ~15 legit-but-uncaptured prod outlets (al-jazeera, espn,
le-monde, …). That CSV<->prod divergence is tracked separately. The seeder is
upsert-only, so it never recreates these 5 rows — this cleanup sticks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from app.config import settings

# dup slug -> canonical slug it duplicates (canonical is the one in the seed CSV)
DUP_TO_CANONICAL = {"bbc": "bbc-news", "bloomberg-news": "bloomberg"}
# excluded aggregator verticals — removed outright (no canonical to keep)
YAHOO = ["yahoo-news", "yahoo-finance", "yahoo-sports"]
TO_REMOVE = list(DUP_TO_CANONICAL) + YAHOO

# Repoint an outlet entity_links ref from a dup slug to its canonical.
_REPOINT_ELINKS = """
UPDATE articles
SET entity_links = (
    SELECT jsonb_agg(
        CASE WHEN e->>'type' = 'outlet' AND e->>'canonical_id' = $1
             THEN jsonb_set(e, '{canonical_id}', to_jsonb($2::text))
             ELSE e END
    )
    FROM jsonb_array_elements(entity_links) e
)
WHERE entity_links @> $3::jsonb
"""

# Remove outlet entity_links refs whose canonical_id is an excluded (Yahoo) slug.
_DROP_ELINKS = """
UPDATE articles
SET entity_links = COALESCE((
    SELECT jsonb_agg(e)
    FROM jsonb_array_elements(entity_links) e
    WHERE NOT (e->>'type' = 'outlet' AND e->>'canonical_id' = ANY($1::text[]))
), '[]'::jsonb)
WHERE EXISTS (
    SELECT 1 FROM jsonb_array_elements(entity_links) e
    WHERE e->>'type' = 'outlet' AND e->>'canonical_id' = ANY($1::text[])
)
"""


def _outlet_ref(slug: str) -> str:
    """JSONB containment probe for an entity_links outlet reference to `slug`."""
    return json.dumps([{"type": "outlet", "canonical_id": slug}])


async def _elink_count(conn: asyncpg.Connection, slug: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM articles WHERE entity_links @> $1::jsonb", _outlet_ref(slug)
    )


async def main(apply: bool) -> None:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    host = urlsplit(db_url).hostname or "?"

    print(f"Target DB host : {host}")
    print(f"Mode          : {'APPLY (writes)' if apply else 'DRY-RUN (read-only)'}\n")

    conn = await asyncpg.connect(db_url, ssl=ssl_mode)
    try:
        total_before = await conn.fetchval("SELECT count(*) FROM outlet_profiles")
        print(f"outlet_profiles total: {total_before}\n")

        # Guard: every canonical target for a dup-repoint must exist, or we'd
        # repoint refs onto a non-existent slug. Abort cleanly if not.
        for dup, canon in DUP_TO_CANONICAL.items():
            if not await conn.fetchval(
                "SELECT 1 FROM outlet_profiles WHERE slug = $1", canon
            ):
                print(f"ABORT: canonical '{canon}' (for dup '{dup}') not found. No changes.")
                return

        present: list[str] = []
        print("Plan:")
        for slug in TO_REMOVE:
            name = await conn.fetchval(
                "SELECT name FROM outlet_profiles WHERE slug = $1", slug
            )
            if name is None:
                print(f"  · {slug:15} not present (already removed) — skip")
                continue
            present.append(slug)
            aliases = [
                r["raw_source_name"]
                for r in await conn.fetch(
                    "SELECT raw_source_name FROM source_name_aliases WHERE outlet_slug = $1",
                    slug,
                )
            ]
            elinks = await _elink_count(conn, slug)
            if slug in DUP_TO_CANONICAL:
                canon = DUP_TO_CANONICAL[slug]
                print(f"  · {slug:15} ({name}) — DUP of '{canon}'")
                print(f"      aliases -> repoint {len(aliases)} to {canon}: {aliases}")
                print(f"      entity_links -> repoint {elinks} article(s) to {canon}")
            else:
                print(f"  · {slug:15} ({name}) — EXCLUDED (Yahoo)")
                print(f"      aliases -> drop {len(aliases)} (cascade): {aliases}")
                print(f"      entity_links -> drop ref in {elinks} article(s)")

        print(f"\nWould remove {len(present)} row(s): {present}")
        print(f"Projected outlet_profiles total: {total_before} -> {total_before - len(present)}")

        if not apply:
            print("\nDRY-RUN — no writes. Re-run with --apply to execute.")
            return

        # ---- APPLY (transactional; any assertion failure rolls back) ----
        async with conn.transaction():
            # 1) Move references off the dups onto their canonical, off Yahoo entirely.
            for dup, canon in DUP_TO_CANONICAL.items():
                a = await conn.execute(
                    "UPDATE source_name_aliases SET outlet_slug = $2 WHERE outlet_slug = $1",
                    dup, canon,
                )
                e = await conn.execute(_REPOINT_ELINKS, dup, canon, _outlet_ref(dup))
                print(f"  {dup} -> {canon}: aliases {a}, entity_links {e}")
            y = await conn.execute(_DROP_ELINKS, YAHOO)
            print(f"  yahoo entity_links dropped: {y}")

            # 2) Delete the rows (cascade clears any residual aliases — Yahoo's).
            d = await conn.execute(
                "DELETE FROM outlet_profiles WHERE slug = ANY($1::text[])", TO_REMOVE
            )
            print(f"  deleted outlet rows: {d}")

            # 3) Verify inside the txn — raise to roll back on any surprise.
            for slug in TO_REMOVE:
                if await conn.fetchval("SELECT 1 FROM outlet_profiles WHERE slug = $1", slug):
                    raise RuntimeError(f"post-delete: '{slug}' still present — rolling back")
                if await _elink_count(conn, slug):
                    raise RuntimeError(f"post-delete: entity_links still ref '{slug}' — rolling back")
            for canon in DUP_TO_CANONICAL.values():
                if not await conn.fetchval("SELECT 1 FROM outlet_profiles WHERE slug = $1", canon):
                    raise RuntimeError(f"post-delete: canonical '{canon}' missing — rolling back")

        total_after = await conn.fetchval("SELECT count(*) FROM outlet_profiles")
        print(f"\nDone. outlet_profiles total: {total_before} -> {total_after}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Issue #91: remove duplicate + excluded (Yahoo) outlet profiles."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Execute writes (default: dry-run, read-only)."
    )
    asyncio.run(main(apply=parser.parse_args().apply))
