"""Refresh `politician_profiles.top_industries_current_cycle` from OpenSecrets.

Phase 3.F.2 — civic-literacy MVP. Reads `data/politician_profiles.csv`,
extracts each politician's CRP (OpenSecrets) ID from
`external_links.opensecrets`, fetches the current-cycle top donor
industries via the OpenSecrets API, and writes the result back to the
CSV's `top_industries_current_cycle` field.

Run from sift-api root:
    OPENSECRETS_API_KEY=... ./.venv/bin/python3 scripts/refresh_politician_donors.py
    OPENSECRETS_API_KEY=... ./.venv/bin/python3 scripts/refresh_politician_donors.py --dry-run
    ./.venv/bin/python3 scripts/refresh_politician_donors.py --max 50

Activation
----------
Set `OPENSECRETS_API_KEY`. Without it, the script logs the absence and
exits cleanly without writing anything — the politician dossier
continues to show "Not yet enriched" until the key arrives.

Free-tier rate limit
--------------------
OpenSecrets free tier is 200 calls/day. With ~535 sitting Congress
members, a full refresh takes ~3 days. Strategies:

  - `--max N` flag caps how many politicians get refreshed in one run.
    The orchestrator processes politicians in CSV order, so a
    quarterly schedule like
        --max 200 --offset 0    (day 1)
        --max 200 --offset 200  (day 2)
        --max 135 --offset 400  (day 3)
    rotates through everyone within quota.

  - Daily cron should pick a different `--offset` each day so all 535
    politicians get refreshed monthly. Phase 3.F.3 wires this up.

Re-run-safe
-----------
- Updates only `top_industries_current_cycle` on matching bioguide_id
  rows. Preserves committees, notes, external_links, party, state,
  chamber, name, etc.
- Empty industries list → leaves the field as `[]` (no overwrite if
  already non-empty), so flaky API responses don't blow away curated
  data on partial reruns.
- Stable JSON formatting → no spurious diffs from re-runs.

Cycle parameter
---------------
Defaults to `2026` (current cycle). Override with `--cycle 2024` to
fetch the previous cycle's totals (useful for testing / historical).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from typing import Any

import httpx

# Add parent dir to path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.opensecrets import (  # noqa: E402
    extract_cid_from_url,
    fetch_top_industries,
)

logger = logging.getLogger("sift-api.refresh-donors")

DEFAULT_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "politician_profiles.csv",
)
DEFAULT_CYCLE = "2026"
DEFAULT_SLEEP_SECONDS = 1.0  # be polite even within quota

# Re-declared here so the script can write back via DictWriter without
# importing the seed module's CSV_FIELDS — keeps coupling minimal.
CSV_FIELDS = [
    "bioguide_id",
    "name",
    "party",
    "state",
    "chamber",
    "committees",
    "top_industries_current_cycle",
    "interest_group_ratings",
    "external_links",
    "notes",
]


def _read_csv(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _write_csv(path: str, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _opensecrets_url_from_row(row: dict[str, str]) -> str | None:
    raw_links = (row.get("external_links") or "").strip()
    if not raw_links:
        return None
    try:
        parsed = json.loads(raw_links)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    url = parsed.get("opensecrets")
    return url if isinstance(url, str) else None


async def _refresh_one(
    row: dict[str, str],
    cycle: str,
    client: httpx.AsyncClient,
    sleep_seconds: float,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Fetch industries for one row. Returns (status, industries).

    status:
      - "skip:no_cid"    → couldn't extract a CRP ID
      - "skip:no_data"   → API returned [], leave existing data alone
      - "ok"             → industries fetched (may be empty list intentionally)
      - "error"          → network / HTTP error (None industries)
    """
    bioguide = row.get("bioguide_id", "").strip()
    cid_url = _opensecrets_url_from_row(row)
    cid = extract_cid_from_url(cid_url)
    if not cid:
        logger.debug("%s: no CRP ID in external_links.opensecrets", bioguide)
        return "skip:no_cid", None

    industries = await fetch_top_industries(cid, cycle=cycle, client=client)
    await asyncio.sleep(sleep_seconds)

    if industries is None:
        return "error", None
    if not industries:
        return "skip:no_data", []
    return "ok", industries


async def main(
    output: str,
    cycle: str,
    max_count: int | None,
    offset: int,
    dry_run: bool,
    sleep_seconds: float,
) -> None:
    rows = _read_csv(output)
    fieldnames = list(rows[0].keys()) if rows else CSV_FIELDS

    api_key = os.environ.get("OPENSECRETS_API_KEY", "").strip()
    if not api_key:
        print(
            "OPENSECRETS_API_KEY not set. Skipping refresh.\n"
            "  Set the env var and re-run to populate top_industries_current_cycle.\n"
            "  See https://www.opensecrets.org/api/admin/index.php to register."
        )
        return

    print(f"OpenSecrets refresh → {output}")
    print(f"  Cycle:  {cycle}")
    print(f"  Rows:   {len(rows)} loaded")
    print(f"  Offset: {offset}")
    if max_count is not None:
        print(f"  Cap:    {max_count} politicians this run")
    if dry_run:
        print("  --dry-run: no CSV writes, no API calls")
    print(f"  Sleep:  {sleep_seconds}s between calls (be polite within the 200/day quota)")

    targets = rows[offset:]
    if max_count is not None:
        targets = targets[:max_count]
    print(f"  → processing {len(targets)} of {len(rows)} politicians")

    if dry_run:
        return

    counts = {"ok": 0, "skip:no_cid": 0, "skip:no_data": 0, "error": 0}
    updated = 0

    async with httpx.AsyncClient(timeout=30) as client:
        for i, row in enumerate(targets):
            bioguide = row.get("bioguide_id", "").strip() or "?"
            name = row.get("name", "").strip() or "?"
            print(f"  [{i + 1}/{len(targets)}] {bioguide} {name}", end="… ")
            status, industries = await _refresh_one(row, cycle, client, sleep_seconds)
            counts[status] += 1
            if status == "ok" and industries:
                # Stable JSON write: separators=(",", ":") avoids whitespace-
                # only churn between Python versions.
                row["top_industries_current_cycle"] = json.dumps(
                    industries, separators=(",", ":"),
                )
                updated += 1
                top1 = industries[0]["industry"] if industries else ""
                print(f"ok ({len(industries)} industries; top: {top1})")
            else:
                print(status)

    print(
        f"\nDone.\n"
        f"  ok:           {counts['ok']}\n"
        f"  skip:no_cid:  {counts['skip:no_cid']}\n"
        f"  skip:no_data: {counts['skip:no_data']}\n"
        f"  error:        {counts['error']}\n"
        f"  → wrote {updated} updated rows back to {output}"
    )

    _write_csv(output, rows, fieldnames)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Refresh politician_profiles donor industries from OpenSecrets.",
    )
    parser.add_argument("--output", default=DEFAULT_CSV_PATH, help="CSV path to update.")
    parser.add_argument("--cycle", default=DEFAULT_CYCLE, help="Election cycle (default 2026).")
    parser.add_argument(
        "--max", dest="max_count", type=int, default=None,
        help="Cap politicians processed in this run (free tier: 200/day).",
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N rows (use to rotate across days).",
    )
    parser.add_argument(
        "--sleep", dest="sleep_seconds", type=float, default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds to sleep between calls (default {DEFAULT_SLEEP_SECONDS}).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write CSV; don't call API.")
    args = parser.parse_args()

    try:
        asyncio.run(main(
            output=args.output,
            cycle=str(args.cycle),
            max_count=args.max_count,
            offset=args.offset,
            dry_run=args.dry_run,
            sleep_seconds=args.sleep_seconds,
        ))
    except KeyboardInterrupt:
        print("\nAborted (CSV may be partially updated).", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
