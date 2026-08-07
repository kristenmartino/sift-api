"""UPSERT the intergovernmental-organization dossiers into org_profiles.

First build under Q8 = "global" (`sift/docs/DECISIONS.md` D47).

Why IGOs rather than more foreign heads of state: a treaty is a fixed
document at a stable URL, so these rows carry the same kind of source the
93 agency rows do (a statute) rather than the kind the 13 foreign
politician rows do (a page that names a person today, and stops being
true when they leave office). Nothing here decays, so nothing here needs
the `role_verified_at` expiry migration 017 added.

They enter `org_profiles` with `type = 'igo'` and reuse the machinery
already there:

  - the sitemap's org rule is type-independent — `governance_structure`
    plus `governance_source` is what publishes a row, so these publish
    with no change to `listSitemapEntries`
  - `/civic` groups by type, so `igo` gets its own section from
    `ORG_TYPE_LABELS` alone
  - `/agencies` filters `type = 'agency'` and is untouched: these are not
    federal agencies and must not be labelled as such

**Refuses to write a row `verify_igo_sources.py` did not mark OK**, and
aborts if that report is older than the CSV — same contract as
`seed_executive_records.py`. Each row's `governance_structure` is a
paraphrase backed by `verify_phrases` quoted from the treaty; the
verifier requires every one of them on the cited page.

Run from sift-api root:

    ./.venv/bin/python3 scripts/verify_igo_sources.py
    railway run ./.venv/bin/python3 scripts/seed_igo_profiles.py --dry-run
    railway run ./.venv/bin/python3 scripts/seed_igo_profiles.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(REPO_ROOT, "data", "igo_profiles.csv")
VERIFICATION = os.path.join(REPO_ROOT, "data", "igo_source_verification.csv")


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        try:
            from app.config import settings
            url = settings.database_url
        except Exception:  # pragma: no cover - config import is best-effort
            pass
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url


def _load_verified() -> set[str]:
    if not os.path.exists(VERIFICATION):
        raise SystemExit(
            f"{VERIFICATION} missing — run scripts/verify_igo_sources.py first"
        )
    if os.path.getmtime(VERIFICATION) < os.path.getmtime(PROFILES):
        raise SystemExit(
            "igo_source_verification.csv is older than igo_profiles.csv — "
            "re-run scripts/verify_igo_sources.py before seeding"
        )
    with open(VERIFICATION, newline="", encoding="utf-8") as fh:
        return {r["slug"] for r in csv.DictReader(fh) if r.get("verdict") == "OK"}


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(PROFILES, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    verified = _load_verified()

    writes, skipped = [], []
    for row in rows:
        slug = row["slug"].strip()
        if slug not in verified:
            skipped.append(f"{slug} — source did not verify")
            continue
        links = {}
        if _clean(row.get("official_url")):
            links["official"] = row["official_url"].strip()
        # The treaty is the citation the page renders; keep it reachable from
        # external_links too, not only from the governance section.
        if _clean(row.get("governance_source")):
            links["founding_document"] = row["governance_source"].strip()
        writes.append((
            slug,
            _clean(row.get("name")),
            _clean(row.get("type")) or "igo",
            _clean(row.get("governance_structure")),
            _clean(row.get("governance_source")),
            json.dumps(links),
        ))

    for line in skipped:
        print(f"  ! {line}", file=sys.stderr)
    print(
        f"\nWould write {len(writes)} of {len(rows)} IGO rows "
        f"(every one carries a verified governance source, so each publishes).",
        file=sys.stderr,
    )
    if args.dry_run:
        print("\n--dry-run: nothing written.", file=sys.stderr)
        return 0

    url = _db_url()
    conn = await asyncpg.connect(
        url, **({"ssl": "require"} if "neon.tech" in url else {})
    )
    try:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO org_profiles
                    (slug, name, type, governance_structure, governance_source,
                     external_links, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())
                ON CONFLICT (slug) DO UPDATE SET
                    name = EXCLUDED.name,
                    type = EXCLUDED.type,
                    governance_structure = EXCLUDED.governance_structure,
                    governance_source = EXCLUDED.governance_source,
                    external_links = org_profiles.external_links
                                     || EXCLUDED.external_links,
                    updated_at = NOW()
                """,
                writes,
            )
        total = await conn.fetchval("SELECT COUNT(*) FROM org_profiles WHERE type = 'igo'")
        cited = await conn.fetchval(
            "SELECT COUNT(*) FROM org_profiles WHERE type = 'igo' "
            "AND governance_structure IS NOT NULL AND governance_source IS NOT NULL"
        )
        print(
            f"\nVerified against the database:\n"
            f"  igo rows:                {total}\n"
            f"  of those, publishable:   {cited} (want {total})",
            file=sys.stderr,
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
