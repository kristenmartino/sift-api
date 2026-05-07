"""Import OpenSecrets PAC industry totals → politician_profiles.csv.

Phase 3.F.2 (bulk-data path) — civic-literacy MVP. Replaces the
discontinued OpenSecrets API with bulk-data import.

What it does
------------
1. Streams `data/opensecrets/pacs22.txt` (PAC contributions to
   candidates, ~758K rows, ~71 MB).
2. Aggregates contribution amounts grouped by `{cid, real_code}`,
   where `real_code` is the CRP industry code OpenSecrets pre-classified
   per contribution. Negative amounts (refunds) net out — final negative
   totals are dropped.
3. Maps industry codes to human names via `data/opensecrets/CRP_Categories.txt`.
4. For each politician in `data/politician_profiles.csv`, looks up the
   top 5 industries by total $ and writes them as JSON into
   `top_industries_current_cycle`.

Run from sift-api root:
    ./.venv/bin/python3 scripts/import_opensecrets_bulk.py
    ./.venv/bin/python3 scripts/import_opensecrets_bulk.py --dry-run
    ./.venv/bin/python3 scripts/import_opensecrets_bulk.py --top-n 10

Inputs (downloaded manually from opensecrets.org/open-data/bulk-data
and stashed in `data/opensecrets/`, gitignored):
  - pacs22.txt           PAC contributions (cycle 2022)
  - CRP_Categories.txt   Industry code → human name

Output:
  - data/politician_profiles.csv updated in place. Only the
    `top_industries_current_cycle` field changes; all other fields
    (committees, notes, external_links, party, etc.) are preserved.
    Politicians with no PAC data → field unchanged (graceful — flaky
    partial input doesn't blow away curated state).

Re-run-safe
-----------
Idempotent. Re-running with the same input produces no diff if the
existing rows already match the computed top-N.

Why pacs22 only, not individual contributions
---------------------------------------------
`indivs22.txt` is 15 GB and would 4x the script complexity for a
modest precision boost. PAC-only totals are the more direct
"institutional industry support" signal — the most editorially
useful slice of donor data for a portfolio dossier. Label the
dossier section accordingly: "Top industries by PAC contributions
(2022 cycle)" rather than "Top donor industries".

Cycle 2022 caveat
-----------------
This is the latest fully-released OpenSecrets bulk data as of May
2026 (released May 2023, ~6 months after the cycle closed). The
2024 cycle hasn't dropped yet. Re-run this script with the new file
once OpenSecrets releases 2024 bulk; the politician dossier copy
should also update from "(2022 cycle)" → "(2024 cycle)".
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

# CRP industry codes are 5 chars: a letter + 4 alphanumeric.
INDUSTRY_CODE_RE = re.compile(r"^[A-Z][0-9A-Z]{4}$")
CRP_CID_RE = re.compile(r"[?&]cid=([A-Z][0-9]{8})")
DEFAULT_TOP_N = 5

# CRP "Sector Long" values that aren't real industries — administrative
# buckets (refunds, party transfers, joint fundraising, unitemized, etc.).
# Codes mapped to these sectors are excluded from the top-N aggregation
# so the dossier doesn't surface "Non-Contribution, Miscellaneous" or
# "Party Committees" as a top "industry."
NON_INDUSTRY_SECTORS: frozenset[str] = frozenset({
    "Party/Non-Contribution",
})

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SIFT_API_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_PACS = os.path.join(SIFT_API_ROOT, "data", "opensecrets", "pacs22.txt")
DEFAULT_CATEGORIES = os.path.join(SIFT_API_ROOT, "data", "opensecrets", "CRP_Categories.txt")
DEFAULT_OUTPUT = os.path.join(SIFT_API_ROOT, "data", "politician_profiles.csv")


def load_categories(path: str) -> tuple[dict[str, str], frozenset[str]]:
    """Parse CRP_Categories.txt → ({Catcode: Catname}, {non-industry codes}).

    The file is TSV with several leading lines of attribution prose
    before the header `Catcode | Catname | Catorder | Industry | Sector | Sector Long`.
    We skip any row whose first column doesn't look like a 5-char CRP code,
    which naturally drops both the header line ("Catcode") and the prose.

    Returns:
      - `code_to_name`: every code's display name, including non-industries
        (so the importer's fallback display still has a label if some weird
        code slips through filtering).
      - `non_industry_codes`: codes whose Sector Long is administrative
        ("Party/Non-Contribution"). The aggregator excludes contributions
        marked with these codes so the dossier doesn't surface
        "Non-Contribution, Miscellaneous" or "Party Committees" as a top
        "industry."
    """
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.", file=sys.stderr)
        sys.exit(1)
    code_to_name: dict[str, str] = {}
    non_industry: set[str] = set()
    with open(path, encoding="latin-1") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 2:
                continue
            catcode = row[0].strip()
            catname = row[1].strip()
            if not INDUSTRY_CODE_RE.match(catcode) or not catname:
                continue
            code_to_name[catcode] = catname
            # Sector Long is the 6th column (index 5) — present only on
            # well-formed rows; admin codes get added to the skip set.
            if len(row) >= 6:
                sector_long = row[5].strip()
                if sector_long in NON_INDUSTRY_SECTORS:
                    non_industry.add(catcode)
    print(
        f"Loaded {len(code_to_name)} industry codes from {os.path.basename(path)}; "
        f"{len(non_industry)} flagged as non-industry (will be filtered)"
    )
    return code_to_name, frozenset(non_industry)


def aggregate_pacs(
    path: str,
    non_industry_codes: frozenset[str],
) -> dict[str, dict[str, int]]:
    """Stream pacs22.txt and aggregate amounts by `{cid → {real_code → total}}`.

    pacs22 columns (pipe-quoted, comma-delimited):
       0: cycle
       1: fec_rec_no
       2: pac_id
       3: cid                 ← politician's CRP ID
       4: amount              ← unquoted integer (may be negative for refunds)
       5: date                ← unquoted MM/DD/YYYY
       6: real_code           ← CRP industry code, pre-classified per contribution
       7: type                ← contribution type code (24K, etc.)
       8: di                  ← Direct/Independent
       9: fec_cand_id

    Three filtering passes:

      1. Skip contributions whose `real_code` is in `non_industry_codes`
         (the administrative sectors — refunds, party transfers, joint
         fundraising, unitemized aggregates, etc.) so they don't pollute
         the top-N.
      2. Negative amounts (refunds, often paired with non-industry codes)
         net into the running total — a contribution-then-refund nets to 0.
      3. After aggregation, drop any `(cid, real_code)` pair with
         `total <= 0`.
    """
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.", file=sys.stderr)
        sys.exit(1)

    totals: dict[str, dict[str, int]] = {}
    rows_processed = 0
    rows_malformed = 0
    rows_non_industry = 0

    print(f"Aggregating {os.path.basename(path)} (this takes 30-60s)…")
    with open(path, encoding="latin-1") as f:
        reader = csv.reader(f, delimiter=",", quotechar="|")
        for row in reader:
            rows_processed += 1
            if len(row) < 7:
                rows_malformed += 1
                continue
            cid = row[3].strip()
            real_code = row[6].strip()
            try:
                amount = int(row[4].strip())
            except (ValueError, IndexError):
                rows_malformed += 1
                continue
            if not cid or not real_code:
                rows_malformed += 1
                continue
            if real_code in non_industry_codes:
                rows_non_industry += 1
                continue
            totals.setdefault(cid, {})
            totals[cid][real_code] = totals[cid].get(real_code, 0) + amount

    # Drop net-negative or zero totals.
    candidates_before = len(totals)
    pairs_before = sum(len(v) for v in totals.values())
    for cid in list(totals.keys()):
        totals[cid] = {code: amt for code, amt in totals[cid].items() if amt > 0}
        if not totals[cid]:
            del totals[cid]
    pairs_after = sum(len(v) for v in totals.values())

    print(f"  Processed {rows_processed:,} contribution rows ({rows_malformed:,} malformed)")
    print(f"  Skipped {rows_non_industry:,} non-industry-coded rows")
    print(
        f"  Aggregated to {candidates_before:,} candidates, "
        f"{pairs_before:,} (candidate, industry) pairs"
    )
    print(
        f"  After dropping net-non-positive: {len(totals):,} candidates, "
        f"{pairs_after:,} pairs"
    )
    return totals


def top_n_industries(
    industry_totals: dict[str, int],
    categories: dict[str, str],
    n: int,
) -> list[dict]:
    """Convert `{industry_code: total}` → top-N `[{industry, amount_usd}]`,
    sorted descending by total. Industry codes missing from the categories
    dictionary fall back to displaying the raw code (rather than dropping
    the entry — at least the reader sees something with a clear "this code
    is unmapped" signal).
    """
    sorted_pairs = sorted(industry_totals.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict] = []
    for code, total in sorted_pairs[:n]:
        industry_name = categories.get(code, code)
        out.append({"industry": industry_name, "amount_usd": int(total)})
    return out


def extract_cid_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = CRP_CID_RE.search(url)
    return m.group(1) if m else None


def main(
    pacs_path: str,
    categories_path: str,
    output_path: str,
    top_n: int,
    dry_run: bool,
) -> None:
    print(f"OpenSecrets bulk import → {output_path}")
    print(f"  pacs:       {pacs_path}")
    print(f"  categories: {categories_path}")
    print(f"  top_n:      {top_n}")
    print()

    categories, non_industry_codes = load_categories(categories_path)
    cid_totals = aggregate_pacs(pacs_path, non_industry_codes)
    print()

    if not os.path.exists(output_path):
        print(
            f"ERROR: {output_path} not found. Run scrape_govtrack.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(output_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    print(f"Loaded {len(rows)} politicians from {os.path.basename(output_path)}")

    updated = 0
    no_change = 0
    skipped_no_cid = 0
    skipped_no_match = 0

    for row in rows:
        external_links_raw = (row.get("external_links") or "").strip()
        cid = None
        if external_links_raw:
            try:
                ext = json.loads(external_links_raw)
                if isinstance(ext, dict):
                    cid = extract_cid_from_url(ext.get("opensecrets"))
            except json.JSONDecodeError:
                pass
        if not cid:
            skipped_no_cid += 1
            continue

        industries = cid_totals.get(cid)
        if not industries:
            skipped_no_match += 1
            continue

        top = top_n_industries(industries, categories, top_n)
        if not top:
            skipped_no_match += 1
            continue

        new_value = json.dumps(top, separators=(",", ":"))
        existing = (row.get("top_industries_current_cycle") or "").strip()
        if existing == new_value:
            no_change += 1
            continue
        row["top_industries_current_cycle"] = new_value
        updated += 1

    print()
    print(
        f"  updated:        {updated}\n"
        f"  no_change:      {no_change}\n"
        f"  no_cid_in_csv:  {skipped_no_cid}\n"
        f"  no_pac_match:   {skipped_no_match}"
    )

    if dry_run:
        print("\n--dry-run set; no CSV writes.")
        return

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"\nWrote {output_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import OpenSecrets PAC industry totals → politician_profiles.csv.",
    )
    parser.add_argument("--pacs", default=DEFAULT_PACS)
    parser.add_argument("--categories", default=DEFAULT_CATEGORIES)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--top-n", type=int, default=DEFAULT_TOP_N,
        help=f"How many top industries to keep per politician (default {DEFAULT_TOP_N}).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        main(
            pacs_path=args.pacs,
            categories_path=args.categories,
            output_path=args.output,
            top_n=args.top_n,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
