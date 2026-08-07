"""Write Phase 4 role provenance onto the executive politician_profiles rows.

Replaces the uncited `notes` prose on all 102 rows with
`chamber IN ('executive','foreign-executive')` with the structured,
primary-record columns migration 015 added, and adds an official .gov
`external_links.official` entry where one exists.

Two things happen in ONE transaction, deliberately:

  1. `notes` is cleared on every one of the 102 rows.
  2. Sourced role fields are written for the rows that have them.

They are not separable. Clearing notes is the fix — those claims violate
`sift/docs/OPERATING_CONTEXT.md` §5 whether or not a replacement exists.
Writing the replacement is what lets a row publish. A row that gets (1)
without (2) keeps rendering and keeps resolving entity chips; it simply
stays out of the sitemap, which is where it already was.

**Refuses to write an unverified role_title.** `verify_role_sources.py`
refetches every `role_title_source` and asserts the record literally names
the office; this script reads that report and drops any row not marked OK.
The report must be newer than the profiles CSV or the run aborts. That
ordering is the whole point — an uncited claim about a living person is
exactly what this migration exists to remove, and "the source was checked
once, by hand, a while ago" is how the Brookings FARA claim survived
(`sift/STATUS.md:80-84`).

`notes` is NOT touched on the 536 sitting-Congress rows, which use it
legitimately.

Run from sift-api root:

    railway run ./.venv/bin/python3 scripts/seed_executive_records.py --dry-run
    railway run ./.venv/bin/python3 scripts/seed_executive_records.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(REPO_ROOT, "data", "executive_profiles.csv")
VERIFICATION = os.path.join(REPO_ROOT, "data", "role_source_verification.csv")

EXEC_CHAMBERS = ("executive", "foreign-executive")

ROLE_COLUMNS = [
    "role_title",
    "role_title_source",
    "role_start_date",
    "role_end_date",
    "role_dates_source",
    "nomination_date",
    "nomination_url",
    "confirmation_date",
    "confirmation_vote_url",
    "confirmation_vote_result",
    "predecessor_name",
    "predecessor_source",
    "role_verified_at",
]
DATE_COLUMNS = {
    "role_start_date",
    "role_end_date",
    "nomination_date",
    "confirmation_date",
    "role_verified_at",
}


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
    """bioguide_id -> the date its role_title_source was refetched and confirmed.

    The date comes from the report, not from `today` at seed time: a CSV
    seeded months after it was verified must not read as freshly checked,
    because the publish floor expires foreign rows on this value (017).
    """
    if not os.path.exists(VERIFICATION):
        raise SystemExit(
            f"{VERIFICATION} missing — run scripts/verify_role_sources.py first"
        )
    if os.path.getmtime(VERIFICATION) < os.path.getmtime(PROFILES):
        raise SystemExit(
            "role_source_verification.csv is older than executive_profiles.csv — "
            "re-run scripts/verify_role_sources.py before seeding"
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
    """asyncpg binds DATE parameters from datetime.date, never from a string.

    A `::date` cast in the SQL does not save you: the driver infers the
    parameter type from the Python object before the server ever sees the
    cast, so a str raises DataError at bind time.
    """
    cleaned = _clean(value)
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        raise SystemExit(
            f"unparseable date in executive_profiles.csv: {cleaned!r}"
        ) from None


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
            "SELECT bioguide_id, chamber, external_links, notes "
            "FROM politician_profiles WHERE chamber = ANY($1::text[])",
            list(EXEC_CHAMBERS),
        )
        live_by_id = {r["bioguide_id"]: r for r in live}
        print(f"{len(live)} executive rows in the database", file=sys.stderr)

        writes: list[tuple] = []
        skipped: list[str] = []
        unknown: list[str] = []

        for row in profiles:
            bioguide = row["bioguide_id"].strip()
            if bioguide not in live_by_id:
                unknown.append(bioguide)
                continue
            if row.get("role_title") and bioguide not in verified:
                skipped.append(f"{bioguide} (role source did not verify)")
                continue

            # role_verified_at is not in the profiles CSV — it comes from the
            # verification report, so the write cannot claim a check that
            # didn't happen.
            row = dict(row, role_verified_at=verified.get(bioguide, ""))
            values = [
                _as_date(row.get(column)) if column in DATE_COLUMNS
                else _clean(row.get(column))
                for column in ROLE_COLUMNS
            ]

            # Keep a more specific official link if the row already has one:
            # 23 rows point at the exact office page (justice.gov/ag), which
            # beats the department root this CSV carries.
            existing = json.loads(live_by_id[bioguide]["external_links"] or "{}")
            official = _clean(row.get("official_url"))
            if official and not existing.get("official"):
                existing["official"] = official
            writes.append(
                (bioguide, row.get("id_source") or "executive", *values,
                 json.dumps(existing))
            )

        for name in unknown:
            print(f"  ! {name} is not an executive row in prod — skipped",
                  file=sys.stderr)
        for name in skipped:
            print(f"  ! {name}", file=sys.stderr)

        publishable = sum(1 for w in writes if w[2] and w[3])
        print(
            f"\nWould write {len(writes)} rows "
            f"({publishable} carry a verified role_title + source, so they "
            f"become eligible for the sitemap).\n"
            f"Would clear `notes` on all {len(live)} executive rows.",
            file=sys.stderr,
        )
        if args.dry_run:
            print("\n--dry-run: nothing written.", file=sys.stderr)
            return 0

        set_clause = ", ".join(
            f"{col} = ${i + 3}" for i, col in enumerate(ROLE_COLUMNS)
        )
        param_n = len(ROLE_COLUMNS) + 3

        async with conn.transaction():
            # 1. The fix: no executive row keeps uncited prose, whether or not
            #    it has a sourced replacement.
            cleared = await conn.execute(
                "UPDATE politician_profiles SET notes = NULL, "
                "id_source = COALESCE(id_source, "
                "  CASE WHEN chamber = 'foreign-executive' "
                "       THEN 'foreign-executive' ELSE 'executive' END), "
                "updated_at = NOW() "
                "WHERE chamber = ANY($1::text[]) "
                "  AND (notes IS NOT NULL OR id_source IS NULL)",
                list(EXEC_CHAMBERS),
            )
            print(f"notes cleared: {cleared}", file=sys.stderr)

            # 2. The replacement.
            await conn.executemany(
                f"UPDATE politician_profiles "
                f"SET id_source = $2, {set_clause}, "
                f"    external_links = ${param_n}::jsonb, updated_at = NOW() "
                f"WHERE bioguide_id = $1",
                writes,
            )
            print(f"role provenance written: {len(writes)}", file=sys.stderr)

        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM politician_profiles "
            "WHERE chamber = ANY($1::text[]) AND notes IS NOT NULL",
            list(EXEC_CHAMBERS),
        )
        eligible = await conn.fetchval(
            "SELECT COUNT(*) FROM politician_profiles "
            "WHERE chamber = ANY($1::text[]) "
            "  AND role_title IS NOT NULL AND role_title_source IS NOT NULL",
            list(EXEC_CHAMBERS),
        )
        print(
            f"\nVerified against prod:\n"
            f"  executive rows still carrying notes: {remaining} (want 0)\n"
            f"  executive rows now sitemap-eligible: {eligible}",
            file=sys.stderr,
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
