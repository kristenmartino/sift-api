"""Are summarizer re-asks caused by output truncation? — ANSWERED, NO.

THE ANSWER, SO NOBODY RE-RUNS THE INVESTIGATION
-----------------------------------------------
Measured 2026-08-11 over the first 212 `summarizer.batch` calls recorded in
`llm_output_stops`:

    0 calls ended in max_tokens (every one end_turn); peak output 481 of 700
    1 misaligned call — 0.5%, not the 4-12% this was premised on

**Truncation is not why batches misalign**, and `MAX_OUTPUT_TOKENS = 700` is
not close to binding at `BATCH_SIZE = 5`. The instrument was checked for a
blind spot and does not have one: a truncated response fails to parse, which
raises `AlignmentError`, which is the path that records `aligned=False`.
Truncation would have been counted. There was none.

**The 4-12% was a measurement artifact**, and it is the more useful finding.
It came from inferring re-asks as the excess of calls over
`ceil(articles / BATCH_SIZE)` in `ai_usage_daily` — which counts every partial
last-batch as a retry:

    articles summarized     1013        actual calls            212
    ceil(articles / 5)       203        'excess' it infers        9  (4.2%)
    ACTUAL misaligned          1  (0.5%)

18 of the 212 calls ran below `BATCH_SIZE` (five at 1 article, six at 2, two
at 3, five at 4), carrying 43 articles that would pack into 9 calls if filled.
That accounts for essentially all of it. Nor is it recoverable waste: packing
across runs would mean holding articles back from a pipeline that runs every
30 minutes.

WHAT THIS SCRIPT IS STILL FOR
-----------------------------
The aligned/misaligned split turned out to be the only stored signal for
whether a model returns parseable indexed JSON *at all* — which is what caught
gpt-5-nano emitting 30/30 empty batches at this same ceiling, spending its
whole budget reasoning, with zero provider errors. Run this when swapping the
model behind any batched operation; the truncation question itself is closed.

WHAT COUNTS AS AN ANSWER
------------------------
Two numbers, and the second is the one that decides:

  truncation rate   share of all calls ending in `max_tokens`
  truncated share   share of MISALIGNED calls ending in `max_tokens`

A high truncation rate among misaligned calls is the finding: the re-asks are
paying to re-roll a response that was cut off, and a larger ceiling ends them.
If misaligned calls stop at `end_turn` like everything else, truncation is not
the cause and the re-asks are genuine scrambling — leave `max_tokens` alone and
stop looking here.

Reports headroom either way: `max_output_tokens` against the ceiling says how
close the ceiling is to binding even when nothing has truncated yet.

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/output_stop_summary.py
    ./.venv/bin/python3 scripts/output_stop_summary.py --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services.summarizer import MAX_OUTPUT_TOKENS  # noqa: E402

# Below this many calls the rate is noise, not a reading. One day of
# summarizer traffic is ~200-500 calls, so this clears in hours, not days —
# it exists to stop a verdict being issued on the first handful of rows, the
# same failure shadow_summary.py's MIN_RUNS guards against.
MIN_CALLS = 100

# Fraction of the ceiling above which the cap is "close to binding" even
# without an outright truncation. Five 105-token summaries plus scaffolding is
# ~82% of 700, so anything at or above this has effectively no room left.
HEADROOM_WARN = 0.80


async def main(days: int) -> int:
    db_url = settings.database_url
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        rows = await conn.fetch("""
            SELECT operation, stop_reason, aligned, batch_size,
                   sum(call_count)::int AS calls,
                   max(max_output_tokens)::int AS peak_out
            FROM llm_output_stops
            WHERE usage_date >= (CURRENT_DATE - $1::int)
            GROUP BY operation, stop_reason, aligned, batch_size
            ORDER BY operation, aligned DESC, calls DESC
        """, days)
    finally:
        await conn.close()

    if not rows:
        print("No llm_output_stops rows. Either the window is wrong, or the "
              "instrumentation is not deployed yet — check that migration 021 "
              "ran (app/db.py:_apply_migrations) and that a pipeline cycle has "
              "completed since.")
        return 1

    print(f"window: last {days} day(s)   ceiling: MAX_OUTPUT_TOKENS = {MAX_OUTPUT_TOKENS}\n")
    print(f"{'operation':22} {'aligned':>7} {'stop_reason':>14} {'batch':>5} "
          f"{'calls':>7} {'peak out':>9}")
    for r in rows:
        print(f"{r['operation']:22} {str(r['aligned']):>7} {r['stop_reason']:>14} "
              f"{r['batch_size']:5d} {r['calls']:7,} {r['peak_out']:9,}")

    total = sum(r["calls"] for r in rows)
    truncated = sum(r["calls"] for r in rows if r["stop_reason"] == "max_tokens")
    misaligned = sum(r["calls"] for r in rows if not r["aligned"])
    mis_trunc = sum(
        r["calls"] for r in rows if not r["aligned"] and r["stop_reason"] == "max_tokens"
    )
    peak = max(r["peak_out"] for r in rows)

    print(f"\ncalls {total:,}   misaligned {misaligned:,} "
          f"({100 * misaligned / total:.1f}%)   truncated {truncated:,} "
          f"({100 * truncated / total:.1f}%)")
    print(f"peak output {peak:,} / {MAX_OUTPUT_TOKENS} "
          f"({100 * peak / MAX_OUTPUT_TOKENS:.0f}% of ceiling)")

    if total < MIN_CALLS:
        print(f"\nHOLD — {total:,} calls is below MIN_CALLS={MIN_CALLS}. Too few to "
              "read a rate off; let another cycle or two land.")
        return 3

    print()
    if misaligned == 0:
        print("No misaligned calls in this window — which is the expected "
              "reading, not a gap. The measured rate is ~0.5% (1 in 212), so "
              f"{total:,} calls buys roughly {total / 212:.1f} expected "
              "misalignments. Treat zero as 'nothing to explain'; the "
              "truncation question is already closed (see the docstring).")
        verdict = 0
    else:
        share = 100 * mis_trunc / misaligned
        print(f"of {misaligned:,} misaligned calls, {mis_trunc:,} were truncated "
              f"({share:.0f}%)")
        if share >= 50:
            print(f"\nTRUNCATION IS THE CAUSE. Raise MAX_OUTPUT_TOKENS — that "
                  f"removes {share:.0f}% of the re-asks and their duplicate cost. "
                  "This is the cheap fix, and it makes the ceiling scale with "
                  "BATCH_SIZE rather than silently binding.")
            verdict = 0
        elif mis_trunc:
            print(f"\nPARTIAL. {share:.0f}% of re-asks are truncation; the rest "
                  "are genuine misalignment. Raising the ceiling is still free "
                  "and still removes that share, but it will not end the retries.")
            verdict = 0
        else:
            print("\nNOT TRUNCATION. Misaligned calls stop the same way aligned "
                  "ones do, so the re-asks are genuine scrambling and a bigger "
                  "ceiling buys nothing. Leave MAX_OUTPUT_TOKENS alone.")
            verdict = 2

    if peak >= HEADROOM_WARN * MAX_OUTPUT_TOKENS:
        print(f"\nSeparately: peak output is {100 * peak / MAX_OUTPUT_TOKENS:.0f}% "
              f"of the ceiling. Even with no truncation yet, a longer-than-usual "
              f"batch would hit it — and raising BATCH_SIZE without raising this "
              f"would truncate immediately.")
    return verdict


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=2, help="lookback window (default 2)")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.days)))
