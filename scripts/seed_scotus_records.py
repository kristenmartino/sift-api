"""Write sourced role provenance onto the nine SCOTUS politician_profiles rows.

Sibling of `seed_executive_records.py`, same discipline, same refusal rules.

Migration 016 moves the rows out of `judge_profiles` into `politician_profiles`
under `chamber = 'scotus'` / `id_source = 'scotus'`, dropping the uncited
`notes` prose and the Wikipedia link on the way. It deliberately does NOT write
replacements — a migration should not depend on a network fetch. This script
writes them, from `data/scotus_confirmations.csv`:

    role_title               <- 28 U.S.C. § 1 (govinfo.gov / GPO)
    role_title_source        <- that URL
    confirmation_date        <- senate.gov roll-call vote menu
    confirmation_vote_result <- same, verbatim tally
    confirmation_vote_url    <- same, the roll-call page itself

`role_dates_source` is set to the roll-call URL, matching migration 015's
comment that for Senate-confirmed officials it "is usually the same roll-call
as confirmation_vote_url". `role_start_date` is NOT set: the commission date,
not the confirmation date, starts the term, and it is not in either record.
NULL renders as nothing; a confirmation date relabelled as a start date would
render as a claim the source does not make.

**Refuses to write an unverified role_title**, exactly as the executive seeder
does: `verify_role_sources.py` refetches every `role_title_source` and asserts
the record literally names the office, and the report must be newer than the
CSV or this aborts. Both office titles here are § 1's own words — "Chief
Justice of the United States" verbatim, and "Associate Justice" as the singular
of its "eight associate justices". The longer form the Senate's recent vote
titles use does not appear in § 1, and the guard rejects it.

Run from sift-api root:

    ./.venv/bin/python3 scripts/verify_role_sources.py \\
        --input data/scotus_confirmations.csv \\
        --report data/scotus_role_verification.csv
    railway run ./.venv/bin/python3 scripts/seed_scotus_records.py --dry-run
    railway run ./.venv/bin/python3 scripts/seed_scotus_records.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(REPO_ROOT, "data", "scotus_confirmations.csv")
VERIFICATION = os.path.join(REPO_ROOT, "data", "scotus_role_verification.csv")

SCOTUS_CHAMBER = "scotus"

# The roster is nine names known in advance. A run that touches a different
# number is wrong by construction — see STATUS.md's active focus on steps that
# report success while producing nothing.
EXPECTED_ROWS = 9


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


def _load_verified() -> dict[str, str]:
    """bioguide_ids whose role_title_source was refetched and confirmed.

    The id column is named `bioguide_id` rather than `canonical_id` because
    that is the column these rows land in (a synthetic Sift id, per migration
    015's id_source) and because verify_role_sources.py reads that key by
    name — `executive_profiles.csv` does the same for its EXEC-* ids.
    """
    if not os.path.exists(VERIFICATION):
        raise SystemExit(
            f"{VERIFICATION} missing — run:\n"
            f"  ./.venv/bin/python3 scripts/verify_role_sources.py "
            f"--input {PROFILES} --report {VERIFICATION}"
        )
    if os.path.getmtime(VERIFICATION) < os.path.getmtime(PROFILES):
        raise SystemExit(
            "scotus_role_verification.csv is older than scotus_confirmations.csv "
            "— re-run scripts/verify_role_sources.py before seeding"
        )
    with open(VERIFICATION, newline="", encoding="utf-8") as fh:
        return {
            row["bioguide_id"]: (row.get("verified_at") or "").strip()
            for row in csv.DictReader(fh)
            if row.get("verdict") == "OK"
        }


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _as_date(value: str | None) -> date | None:
    """asyncpg binds DATE parameters from datetime.date, never from a string."""
    cleaned = _clean(value)
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        raise SystemExit(f"unparseable date in {PROFILES}: {cleaned!r}") from None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(PROFILES, newline="", encoding="utf-8") as fh:
        profiles = list(csv.DictReader(fh))
    verified = _load_verified()

    url = _db_url()
    conn = await asyncpg.connect(
        url, **({"ssl": "require"} if "neon.tech" in url else {})
    )
    try:
        live = await conn.fetch(
            "SELECT bioguide_id, name, notes FROM politician_profiles "
            "WHERE chamber = $1",
            SCOTUS_CHAMBER,
        )
        live_ids = {r["bioguide_id"] for r in live}
        print(f"{len(live)} scotus rows in the database", file=sys.stderr)

        if len(live) != EXPECTED_ROWS:
            print(
                f"  ! expected {EXPECTED_ROWS} — has migration 016 run against "
                f"this database?",
                file=sys.stderr,
            )

        writes: list[tuple] = []
        skipped: list[str] = []

        for row in profiles:
            canonical_id = row["bioguide_id"].strip()
            if canonical_id not in live_ids:
                skipped.append(f"{canonical_id} is not a scotus row in this DB")
                continue
            if canonical_id not in verified:
                skipped.append(f"{canonical_id} role source did not verify")
                continue
            vote_url = _clean(row.get("confirmation_vote_url"))
            # The PN record sources BOTH the nomination date and the "vice
            # <name>" predecessor. Paired here so neither can be written
            # without it — a Justice's seat is always filled vice a named
            # predecessor, so unlike the executive rows this clause is the
            # primary record rather than a fallback.
            nomination_url = _clean(row.get("nomination_url"))
            writes.append((
                canonical_id,
                _clean(row.get("role_title")),
                _clean(row.get("role_title_source")),
                _as_date(row.get("confirmation_date")),
                vote_url,
                _clean(row.get("confirmation_vote_result")),
                vote_url,  # role_dates_source — the same roll-call record
                _as_date(row.get("nomination_date")) if nomination_url else None,
                nomination_url,
                _clean(row.get("predecessor_name")) if nomination_url else None,
                _clean(row.get("predecessor_source")),
                _as_date(verified.get(canonical_id, "")),
            ))

        for line in skipped:
            print(f"  ! {line}", file=sys.stderr)

        print(
            f"\nWould write role provenance for {len(writes)} of "
            f"{EXPECTED_ROWS} Justices.",
            file=sys.stderr,
        )
        if len(writes) != EXPECTED_ROWS:
            print(
                "Refusing: the roster is known in advance, so a partial write "
                "is a failure, not a result.",
                file=sys.stderr,
            )
            return 1
        if args.dry_run:
            print("\n--dry-run: nothing written.", file=sys.stderr)
            return 0

        async with conn.transaction():
            await conn.executemany(
                "UPDATE politician_profiles SET "
                "  role_title = $2, role_title_source = $3, "
                "  confirmation_date = $4, confirmation_vote_url = $5, "
                "  confirmation_vote_result = $6, role_dates_source = $7, "
                "  nomination_date = $8, nomination_url = $9, "
                "  predecessor_name = $10, predecessor_source = $11, "
                "  role_verified_at = $12, "
                "  notes = NULL, updated_at = NOW() "
                "WHERE bioguide_id = $1",
                writes,
            )
            print(f"role provenance written: {len(writes)}", file=sys.stderr)

        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM politician_profiles "
            "WHERE chamber = $1 AND notes IS NOT NULL",
            SCOTUS_CHAMBER,
        )
        eligible = await conn.fetchval(
            "SELECT COUNT(*) FROM politician_profiles "
            "WHERE chamber = $1 "
            "  AND role_title IS NOT NULL AND role_title_source IS NOT NULL",
            SCOTUS_CHAMBER,
        )
        wiki = await conn.fetchval(
            "SELECT COUNT(*) FROM politician_profiles "
            "WHERE chamber = $1 AND external_links ? 'wikipedia'",
            SCOTUS_CHAMBER,
        )
        print(
            f"\nVerified against the database:\n"
            f"  scotus rows still carrying notes:     {remaining} (want 0)\n"
            f"  scotus rows now sitemap-eligible:     {eligible} (want {EXPECTED_ROWS})\n"
            f"  scotus rows still linking wikipedia:  {wiki} (want 0)",
            file=sys.stderr,
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
