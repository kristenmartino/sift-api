"""Check whether the Neon compute is actually scaling to zero, and what it costs.

WHY THIS EXISTS
---------------
Neon compute was billed as if it ran 24/7 and nobody noticed for months.
`docs/DECISIONS.md` in the `sift` repo still said "Neon = $0 (free tier)" long
after the project had moved to Launch, and the storage post-mortem in
`docs/NEON_RETENTION.md` was written about a bill that storage was not
actually driving.

Measured 2026-08-14: `pg_postmaster_start_time()` reported **26 days of
unbroken uptime**. The compute had never once suspended. Cause: the batch
poller ran a SELECT against `api_batches` every 60 seconds forever, and
`/health` ran two more every 30 minutes from the GitHub heartbeat — all inside
Neon's 300s scale-to-zero window, resetting it 1,440+ times a day.

**Scale-to-zero was enabled the whole time, and the autoscale floor was
already 0.25 CU.** Both were checked in the console on 2026-08-14 and needed no
change. That is what makes the diagnosis conclusive rather than plausible: with
suspension enabled, 26 days of unbroken uptime can only mean a query arrived
inside every single 5-minute window.

**On the billing model:** Launch bills compute usage-based from the first
hour — there is no included-CU-hour allowance to get under, so savings are
linear and every avoided wake is money. Observed rate 2026-08-14: 312.8 CU-h →
$33.08, i.e. ~$0.106/CU-hour. Rates change; re-read them from the billing page
rather than trusting COMPUTE_RATE_USD below.

Same lesson as `verify_cost_baseline.py`: the fix for a number going stale is
not to write a better number, it is to make re-deriving it a one-liner.

HOW TO READ THE OUTPUT
----------------------
`--probe` is the cheap answer and needs no API key. It reads
`pg_postmaster_start_time()`, which changes **only when the compute restarts**
— and a resume from suspend is a restart. So:

    uptime measured in days   -> the compute is not suspending. Investigate.
    uptime < the gap between  -> it suspended and resumed since you last looked.
      pipeline runs (30 min)

Caveat: changing a setting in the Neon console also restarts the compute. Do
not take a reading immediately after flipping scale-to-zero or autoscale
bounds — wait out one quiet window first.

**Do not put --probe in a loop tighter than the scale-to-zero window.** The
probe is itself a query, so polling every minute — or every five — holds the
compute open and the measurement reports "never suspends" no matter what the
code does. That is the same defect this script exists to find, committed by
the tool looking for it. Take ONE reading, well after a quiet period. `--watch`
samples at 7-minute intervals for exactly this reason: long enough that the
compute can suspend between samples, which is what produces the cold-start
sawtooth it looks for.

`--api` is authoritative: it pulls the same consumption history the invoice is
computed from. Needs NEON_API_KEY (Neon console -> Account settings -> API
keys; read-only is sufficient) and NEON_PROJECT_ID.

Exit codes: 0 ok, 1 projected over --budget CU-hours, 2 could not measure.

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/verify_neon_idle.py --probe
    ./.venv/bin/python3 scripts/verify_neon_idle.py --api --days 7
    ./.venv/bin/python3 scripts/verify_neon_idle.py --probe --watch 20
    ./.venv/bin/python3 scripts/verify_neon_idle.py --api --json data/_cache/neon.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from os.path import abspath, dirname

sys.path.insert(0, dirname(dirname(abspath(__file__))))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

from app.config import settings  # noqa: E402

# Launch bills compute from the first hour — there is no free allowance, so
# this is a self-imposed budget, not a plan limit. Default is roughly what this
# project should cost once the compute suspends between pipeline runs: ~5.4
# CU-h/day. Override with --budget.
#
# NOTE: the org's CU-hours are shared across every project in it (4 as of
# 2026-08-14, of which sift was 139 of 313 CU-h). This script measures ONE
# project. A green result here does not mean the invoice is green.
DEFAULT_BUDGET_CU_HOURS = 175

# This script deliberately reports CU-hours and never dollars. A rate constant
# here would be one more written-down number with no owner — the exact defect
# this whole exercise was about — and tests/test_cost_estimates.py already
# forbids `*_USD` constants outside services/cost_estimates.py for that reason.
# For dollars, read the billing page, which is authoritative and always current:
BILLING_URL = "https://console.neon.tech/app/billing"

NEON_API = "https://console.neon.tech/api/v2"


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", settings.database_url)
    # channel_binding is a libpq option asyncpg does not accept in the DSN.
    return url.replace("&channel_binding=require", "").replace("?channel_binding=require", "")


async def _connect() -> asyncpg.Connection:
    url = _db_url()
    # Same handling as scripts/explain_feed_queries.py — Neon requires SSL,
    # a local Postgres must not be asked for it.
    ssl_mode = "require" if "neon.tech" in url else False
    return await asyncpg.connect(url, ssl=ssl_mode)


async def probe() -> dict:
    """One connect + one read. Returns connect latency and compute uptime."""
    started = time.monotonic()
    conn = await _connect()
    connect_ms = (time.monotonic() - started) * 1000
    try:
        row = await conn.fetchrow(
            "SELECT pg_postmaster_start_time() AS started, "
            "       now() - pg_postmaster_start_time() AS uptime, "
            "       pg_size_pretty(pg_database_size(current_database())) AS db_size"
        )
    finally:
        await conn.close()

    uptime: timedelta = row["uptime"]
    return {
        "connect_ms": round(connect_ms, 1),
        "compute_started_at": row["started"].isoformat(),
        "uptime_seconds": int(uptime.total_seconds()),
        "uptime_human": str(uptime),
        "db_size": row["db_size"],
    }


def _render_probe(result: dict) -> int:
    hours = result["uptime_seconds"] / 3600
    print(f"connect latency   : {result['connect_ms']} ms")
    print(f"compute booted at : {result['compute_started_at']}")
    print(f"compute uptime    : {result['uptime_human']}")
    print(f"database size     : {result['db_size']}")
    print()

    # The scheduled pipeline runs every 30 min, so a compute that suspends
    # cannot accumulate much more than that between resumes. Two hours is a
    # deliberately loose ceiling — it tolerates a manual refresh or a backfill
    # without crying wolf, while still catching "never suspends".
    if hours > 2:
        print(
            f"NOT SUSPENDING: {hours:.1f}h of unbroken uptime. Something is querying "
            "inside the 300s scale-to-zero window."
        )
        print("  Check, in likelihood order:")
        print("   1. A polling loop querying on a timer under 300s. This is what it")
        print("      was in Aug 2026 (the batch poller, every 60s) and it is the")
        print("      only cause that survives Scale to Zero being enabled.")
        print("   2. An uptime monitor hitting siftnews.io? Every page render runs")
        print("      sift/lib/db.ts against this same compute.")
        print("   3. Scale to Zero disabled on the branch's compute endpoint. Least")
        print("      likely — it was verified ON 2026-08-14 — but free to re-check.")
        return 1

    print(f"OK: {hours:.2f}h uptime — the compute is suspending and resuming.")
    return 0


async def watch(minutes: int) -> int:
    """Sample connect latency over a quiet window.

    A flat profile means the compute never suspended. A sawtooth — one slow
    connect after an idle gap, then fast ones — means it did. Run this with
    nothing else touching the database.
    """
    print(f"Sampling every 7 minutes for {minutes} minutes. Keep the DB quiet.\n")
    print(f"{'elapsed':>8}  {'connect_ms':>11}  {'uptime':>20}")
    print("-" * 45)

    deadline = time.monotonic() + minutes * 60
    samples: list[float] = []
    start = time.monotonic()
    while True:
        result = await probe()
        samples.append(result["connect_ms"])
        elapsed = int(time.monotonic() - start)
        print(f"{elapsed:>7}s  {result['connect_ms']:>11}  {result['uptime_human']:>20}")
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(420, max(0, deadline - time.monotonic())))

    if len(samples) < 2:
        print("\nNeed at least two samples to say anything.")
        return 2

    spread = max(samples) / min(samples)
    print()
    if spread > 2:
        print(f"OK: {spread:.1f}x spread in connect latency — consistent with cold starts.")
        return 0
    print(
        f"Flat profile ({spread:.1f}x spread). Either the compute never suspended, "
        "or the window was too short to cross the 300s threshold."
    )
    return 1


async def consumption(days: int) -> dict:
    """Pull per-hour compute consumption — the numbers the invoice is built from."""
    key = os.environ.get("NEON_API_KEY")
    project = os.environ.get("NEON_PROJECT_ID")
    if not key or not project:
        raise RuntimeError(
            "NEON_API_KEY and NEON_PROJECT_ID must be set for --api. Create a "
            "read-only key at: Neon console -> Account settings -> API keys."
        )

    to = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    frm = to - timedelta(days=days)

    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.get(
            f"{NEON_API}/consumption_history/projects",
            headers={"Authorization": f"Bearer {key}"},
            params={
                "project_ids": project,
                "from": frm.isoformat().replace("+00:00", "Z"),
                "to": to.isoformat().replace("+00:00", "Z"),
                "granularity": "hourly",
            },
        )
        resp.raise_for_status()
        payload = resp.json()

    periods = payload.get("projects", [{}])[0].get("periods", [])
    buckets = [c for p in periods for c in p.get("consumption", [])]

    compute_seconds = sum(b.get("compute_time_seconds", 0) for b in buckets)
    active_seconds = sum(b.get("active_time_seconds", 0) for b in buckets)
    hours_observed = max(len(buckets), 1)

    cu_hours = compute_seconds / 3600
    return {
        "window_days": days,
        "hours_observed": hours_observed,
        "cu_hours": round(cu_hours, 1),
        "active_hours": round(active_seconds / 3600, 1),
        # active_time is wall-clock awake; compute_time is that scaled by CU
        # size. Their ratio is the effective CU the compute actually ran at.
        "effective_cu": round(compute_seconds / active_seconds, 2) if active_seconds else None,
        "duty_cycle_pct": round(100 * (active_seconds / 3600) / hours_observed, 1),
        "projected_cu_hours_per_month": round(cu_hours / hours_observed * 730, 1),
    }


def _render_consumption(result: dict, budget: int) -> int:
    projected = result["projected_cu_hours_per_month"]
    print(f"window            : {result['window_days']}d ({result['hours_observed']}h of data)")
    print(f"compute consumed  : {result['cu_hours']} CU-hours")
    print(f"wall-clock awake  : {result['active_hours']} h  ({result['duty_cycle_pct']}% duty cycle)")
    if result["effective_cu"] is not None:
        print(f"effective size    : {result['effective_cu']} CU while awake")
    print()
    print(f"projected/month   : {projected} CU-hours   budget {budget}")
    print(f"                    for dollars: {BILLING_URL}")
    print("                    (that bill covers every project in the org; this is one)")

    if projected > budget:
        over = projected - budget
        print()
        print(f"OVER budget by {over:.0f} CU-hours/month.")
        print("  Levers, in the order they usually pay off:")
        print("   1. Is anything querying on a timer? Compute billed while idle is")
        print("      the whole game — check the duty cycle above, not the total.")
        print("   2. Cut the number of wakes. Each costs its work PLUS a fixed 300s")
        print("      tail, so at short run times the tail is most of the bill.")
        print("   3. Lower the autoscale minimum (0.25 CU bills a quarter as fast).")
        return 1

    print(f"OK: {budget - projected:.0f} CU-hours/month under budget.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--probe", action="store_true",
                        help="connect + read pg_postmaster_start_time (no API key needed)")
    parser.add_argument("--api", action="store_true",
                        help="pull consumption history from the Neon API (needs NEON_API_KEY)")
    parser.add_argument("--watch", type=int, metavar="MINUTES",
                        help="sample connect latency over a quiet window")
    parser.add_argument("--days", type=int, default=7,
                        help="lookback window for --api (default 7)")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET_CU_HOURS,
                        help=f"CU-hours/month this project should stay under "
                             f"(default {DEFAULT_BUDGET_CU_HOURS}; self-imposed, "
                             f"Launch has no free allowance)")
    parser.add_argument("--json", metavar="PATH", help="also write results as JSON")
    args = parser.parse_args()

    if not (args.probe or args.api or args.watch):
        args.probe = True

    out: dict = {}
    status = 0

    try:
        if args.watch:
            status = max(status, asyncio.run(watch(args.watch)))
        elif args.probe:
            out["probe"] = asyncio.run(probe())
            status = max(status, _render_probe(out["probe"]))

        if args.api:
            if out:
                print()
            out["consumption"] = asyncio.run(consumption(args.days))
            status = max(status, _render_consumption(out["consumption"], args.budget))
    except Exception as e:
        print(f"Could not measure: {e}", file=sys.stderr)
        return 2

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nWrote {args.json}")

    return status


if __name__ == "__main__":
    raise SystemExit(main())
