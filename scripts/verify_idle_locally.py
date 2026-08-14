"""Prove, against a real Postgres, that an idle process holds nothing open.

WHY THIS EXISTS
---------------
`CLAUDE.md` carries a rule: never add a polling loop that queries Postgres on a
timer shorter than 300s, because Neon's compute cannot scale to zero while one
exists. In Aug 2026 the batch poller did exactly that for months — a `SELECT`
on `api_batches` every 60 seconds, before checking whether anything was
pending — and the prod compute ran 26 days without once suspending.

`tests/test_batch_poller_idle.py` asserts the same property against a mock
pool, which is the right thing for CI. It cannot catch the failure that
actually costs money: a loop somewhere else in the process, or a pool that
never releases. This does, by starting the real background tasks against a real
database and watching `pg_stat_activity` from a separate connection.

The companion is `verify_neon_idle.py`, which measures the same property in
production after a deploy. Use this one before.

HOW TO READ THE OUTPUT
----------------------
A connection at t+35s is CORRECT, not a leak: the last query is the poller's
startup recovery read at ~t+3s, and asyncpg's inactive-connection timer
(`max_inactive_connection_lifetime`, 60s in app/db.py) does not expire until
~t+63s.

The two samples that matter are t+70s and t+100s. Both must be zero:

  t+70s  == 0   the pool drains at all
  t+100s == 0   nothing brought it back

The old poller fired at 60s and every 60s after, so a connection at either
point means something is still on a timer. That is the regression this catches.

Takes ~100 seconds by construction — it has to span two 60-second boundaries —
so it is a manual check, not a CI job.

Usage (from sift-api root, with a local Postgres running):

    ./.venv/bin/python3 scripts/verify_idle_locally.py
    ./.venv/bin/python3 scripts/verify_idle_locally.py --dsn postgresql://...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from os.path import abspath, dirname

sys.path.insert(0, dirname(dirname(abspath(__file__))))

import asyncpg  # noqa: E402

DEFAULT_DSN = "postgresql://sift:sift@localhost:5432/siftdb"

# (label, seconds to sleep before sampling). The first is inside the idle
# window on purpose — see the docstring.
SAMPLES = (("t+35s", 32), ("t+70s", 35), ("t+100s", 30))


async def run(dsn: str) -> int:
    # Set before importing app.db: it reads settings at import time, and this
    # must never point at production — the whole check is "does it go quiet",
    # which waking prod would answer wrongly.
    os.environ["DATABASE_URL"] = dsn
    os.environ["ENVIRONMENT"] = "development"

    from app.db import close_pool, init_pool
    from services import batch_client
    from services.batch_poller import run_batch_poller

    dbname = dsn.rsplit("/", 1)[-1].split("?")[0]
    watcher = await asyncpg.connect(dsn)
    my_pid = await watcher.fetchval("SELECT pg_backend_pid()")

    async def app_conns() -> int:
        return await watcher.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND pid <> $2",
            dbname,
            my_pid,
        )

    await init_pool()
    print(f"after init_pool + migrations   : {await app_conns()} app conn(s)")

    task = asyncio.create_task(run_batch_poller())
    await asyncio.sleep(3)
    print(f"t+3s   (recovery read done)    : {await app_conns()} app conn(s)")
    print(f"        poller has pending?    : {batch_client.has_pending()}")

    counts: list[int] = []
    for label, delay in SAMPLES:
        await asyncio.sleep(delay)
        n = await app_conns()
        counts.append(n)
        print(f"{label:<30} : {n} app conn(s)")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await watcher.close()

    print()
    if counts[1] != 0:
        print("FAIL: pool never drained — still connected 60s+ after the last query.")
        print("      Check max_inactive_connection_lifetime in app/db.py.")
        return 1
    if counts[2] != 0:
        print("FAIL: a connection reappeared past the 60s mark.")
        print("      Something is querying on a timer. That is the whole defect;")
        print("      find the loop before this ships.")
        return 1

    print("PASS: pool drained to zero and stayed there across two 60s boundaries.")
    print("      An idle process holds no connection and issues no query, so the")
    print("      Neon compute is free to scale to zero.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        help=f"local Postgres to test against (default {DEFAULT_DSN})",
    )
    args = parser.parse_args()

    if "neon.tech" in args.dsn:
        print("Refusing to run against Neon — this needs a local database.", file=sys.stderr)
        return 2

    return asyncio.run(run(args.dsn))


if __name__ == "__main__":
    raise SystemExit(main())
