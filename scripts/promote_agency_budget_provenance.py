"""Move the agencies' budget citation into the columns that gate rendering.

23 federal-agency rows carry an `annual_budget_usd` that both surfaces now
withhold, because `annual_budget_fy` and `annual_budget_source` are NULL. They
are not actually unsourced. The provenance is in `external_links`:

    budget_source              https://www.whitehouse.gov/.../hist04z1_fy2027.xlsx
    budget_source_fiscal_year  FY2025

That data was written 2026-05-20 (commit 9f44ba2, "OMB FY2025 actuals enriched
onto 23 agencies via Historical Tables Table 4.1"). Migration 013 created the
dedicated columns on 2026-07-28 — nine weeks later — and nothing backfilled
the older rows into them. So the render gates in `sift/lib/org.ts` and
`sift-mcp`'s `gate_org_claims` correctly withhold a figure whose citation they
cannot see, even though the citation exists.

This promotes it. `external_links` keys are left in place: they are the
historical record of where the value came from, and other tooling reads them.

**Every figure is re-verified against the primary record before it is
written.** The script downloads Table 4.1, parses the FY2025 column, and
requires the stored value to equal the table's to the dollar. A row that does
not match is refused, not promoted — promoting a citation onto a number the
source does not support is worse than leaving the number withheld, and is the
exact failure migration 013 removed (ten hand-authored think-tank figures,
every one wrong).

The table is published in millions; `annual_budget_usd` is in dollars.

Two label mappings are not literal and are worth knowing:

  - **USAID** maps to "International Assistance Programs". USAID has no line
    of its own in Table 4.1.
  - **SSA** maps to "Social Security Administration (Off-Budget)" —
    $1,520,918M, the OASDI trust funds — NOT the (On-Budget) line, which is
    $125,595M and covers SSI plus administration. The two are separate rows in
    the table and this figure is one of them, not their sum. Whether the
    headline number for "SSA's budget" should be Off-Budget alone is an
    editorial question this script does not decide; it only certifies that the
    stored figure is exactly what the cited line says.

Run from sift-api root:

    ./.venv/bin/python3 scripts/promote_agency_budget_provenance.py --dry-run
    railway run ./.venv/bin/python3 scripts/promote_agency_budget_provenance.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

USER_AGENT = "SiftNews/1.0 (civic dossier sourcing; +https://siftnews.kristenmartino.ai)"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# The fiscal-year column to read out of Table 4.1, matching the
# `budget_source_fiscal_year` the rows already carry.
FISCAL_YEAR = "2025"

# org_profiles.slug -> the verbatim "Department or other unit" label in
# Table 4.1. A wrong mapping cannot silently corrupt anything: the value check
# below compares against the mapped row and refuses on mismatch.
OMB_LABEL = {
    "united-states-department-of-agriculture": "Department of Agriculture",
    "united-states-department-of-commerce": "Department of Commerce",
    "united-states-department-of-defense": "Department of Defense--Military Programs",
    "united-states-department-of-education": "Department of Education",
    "united-states-department-of-energy": "Department of Energy",
    "united-states-department-of-health-and-human-services": "Department of Health and Human Services",
    "united-states-department-of-homeland-security": "Department of Homeland Security",
    "united-states-department-of-housing-and-urban-development": "Department of Housing and Urban Development",
    "united-states-department-of-the-interior": "Department of the Interior",
    "united-states-department-of-justice": "Department of Justice",
    "united-states-department-of-labor": "Department of Labor",
    "united-states-department-of-state": "Department of State",
    "united-states-department-of-transportation": "Department of Transportation",
    "united-states-department-of-the-treasury": "Department of the Treasury",
    "united-states-department-of-veterans-affairs": "Department of Veterans Affairs",
    "environmental-protection-agency": "Environmental Protection Agency",
    "general-services-administration": "General Services Administration",
    "national-aeronautics-and-space-administration": "National Aeronautics and Space Administration",
    "national-science-foundation": "National Science Foundation",
    "office-of-personnel-management": "Office of Personnel Management",
    "small-business-administration": "Small Business Administration",
    # See the module docstring — neither of these is a literal name match.
    "united-states-agency-for-international-development": "International Assistance Programs",
    "social-security-administration": "Social Security Administration (Off-Budget)",
}


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        from app.config import settings
        url = settings.database_url
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url


def load_table(url: str) -> dict[str, float]:
    """{agency label: FY outlay in millions} straight from OMB's workbook.

    Parsed with the standard library rather than openpyxl: an xlsx is a zip of
    XML, this needs two files out of it, and the dependency is not worth
    adding to a script that runs by hand.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    book = zipfile.ZipFile(BytesIO(blob))

    shared = [
        "".join(t.text or "" for t in si.iter(f"{NS}t"))
        for si in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(f"{NS}si")
    ]
    sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))

    grid: list[dict[str, str]] = []
    for row in sheet.iter(f"{NS}row"):
        cells: dict[str, str] = {}
        for cell in row.iter(f"{NS}c"):
            col = re.match(r"([A-Z]+)", cell.get("r")).group(1)
            value = cell.find(f"{NS}v")
            if value is None:
                continue
            cells[col] = (
                shared[int(value.text)] if cell.get("t") == "s" else value.text
            )
        if cells:
            grid.append(cells)

    header = next(
        (r for r in grid if (r.get("A") or "").startswith("Department or other unit")),
        None,
    )
    if header is None:
        raise SystemExit("Table 4.1: could not find the header row")
    column = next((k for k, v in header.items() if v == FISCAL_YEAR), None)
    if column is None:
        raise SystemExit(f"Table 4.1: no column for FY{FISCAL_YEAR}")

    out: dict[str, float] = {}
    for row in grid:
        label, raw = (row.get("A") or "").strip(), row.get(column)
        if not label or raw is None:
            continue
        try:
            out[label] = float(raw)
        except ValueError:
            continue
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    url = _db_url()
    conn = await asyncpg.connect(
        url, **({"ssl": "require"} if "neon.tech" in url else {})
    )
    try:
        rows = await conn.fetch(
            "SELECT slug, name, annual_budget_usd, external_links "
            "  FROM org_profiles "
            " WHERE annual_budget_usd IS NOT NULL "
            "   AND (annual_budget_fy IS NULL OR annual_budget_source IS NULL) "
            " ORDER BY slug"
        )
        print(f"{len(rows)} rows carry a withheld budget figure\n", file=sys.stderr)
        if not rows:
            return 0

        # Every row should cite the same workbook; read it once, from the URL
        # the data itself names rather than one hardcoded here.
        sources = set()
        for row in rows:
            links = row["external_links"]
            links = links if isinstance(links, dict) else json.loads(links or "{}")
            sources.add(links.get("budget_source"))
        sources.discard(None)
        if len(sources) != 1:
            print(f"expected one budget_source, found {len(sources)}: {sources}",
                  file=sys.stderr)
            return 1
        source_url = sources.pop()
        print(f"verifying against {source_url}", file=sys.stderr)
        table = load_table(source_url)
        print(f"Table 4.1 parsed: {len(table)} agency lines, FY{FISCAL_YEAR}\n",
              file=sys.stderr)

        writes: list[tuple] = []
        refused: list[str] = []
        for row in rows:
            slug = row["slug"]
            links = row["external_links"]
            links = links if isinstance(links, dict) else json.loads(links or "{}")
            fy = (links.get("budget_source_fiscal_year") or "").strip()
            label = OMB_LABEL.get(slug)
            if not label:
                refused.append(f"{slug}: no OMB label mapping")
                continue
            if label not in table:
                refused.append(f"{slug}: '{label}' not in Table 4.1")
                continue
            if not fy:
                refused.append(f"{slug}: no budget_source_fiscal_year")
                continue

            stored = float(row["annual_budget_usd"])
            official = table[label] * 1_000_000
            if abs(stored - official) >= 1.0:
                refused.append(
                    f"{slug}: stored ${stored:,.0f} != Table 4.1 ${official:,.0f} "
                    f"['{label}'] — NOT promoted"
                )
                continue
            writes.append((slug, fy, source_url))
            print(f"  OK  {slug:52} ${stored/1e6:>12,.0f}M  {fy}", file=sys.stderr)

        for line in refused:
            print(f"  !   {line}", file=sys.stderr)

        print(f"\n{len(writes)} verified against the primary record, "
              f"{len(refused)} refused.", file=sys.stderr)
        if args.dry_run:
            print("\n--dry-run: nothing written.", file=sys.stderr)
            return 0
        if not writes:
            return 1

        async with conn.transaction():
            await conn.executemany(
                "UPDATE org_profiles "
                "   SET annual_budget_fy = $2, annual_budget_source = $3, "
                "       updated_at = NOW() "
                " WHERE slug = $1",
                writes,
            )
        print(f"promoted: {len(writes)}", file=sys.stderr)

        withheld = await conn.fetchval(
            "SELECT count(*) FROM org_profiles "
            " WHERE annual_budget_usd IS NOT NULL "
            "   AND (annual_budget_fy IS NULL OR annual_budget_source IS NULL)"
        )
        renders = await conn.fetchval(
            "SELECT count(*) FROM org_profiles "
            " WHERE annual_budget_usd IS NOT NULL "
            "   AND annual_budget_fy IS NOT NULL AND annual_budget_source IS NOT NULL"
        )
        print(
            f"\nVerified against the database:\n"
            f"  budgets still withheld: {withheld} (want {len(refused)})\n"
            f"  budgets that render:    {renders}",
            file=sys.stderr,
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
