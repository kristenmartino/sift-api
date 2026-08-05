"""Compare current Anthropic/Voyage spend against the pre-optimization baseline.

WHY THIS EXISTS
---------------
`STATUS.md:40` carried "~$15/mo" for weeks while the real figure was ~$300/mo,
because `usage_tracker._record_to_ledger` short-circuits unless
`ai_cost_guard_enabled` is true and it defaulted false — so `ai_usage_daily`
was empty and nobody could tell. The fix for a number going stale is not to
write a better number; it is to make re-deriving it a one-liner.

Run this 48h after any cost-affecting deploy, and whenever STATUS.md's cost
bullet is about to be quoted.

WHAT IT CHECKS
--------------
Per-operation $/day and calls/day, current window vs. the frozen baseline
below, plus a **deploy check**: the two 2026-08-05 changes are visible in call
*ratios*, not just dollars, so the script can tell "not deployed yet" apart
from "deployed and saved nothing" — which a dollars-only diff cannot.

  entity_linker_llm.link_text   ~1 call/article  -> ~0.26 (PR #130 regex gate)
  story_synthesizer.synthesize  ~4.4 per cluster -> ~2.0 (PR #129 reuse skip)

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/verify_cost_baseline.py
    ./.venv/bin/python3 scripts/verify_cost_baseline.py --since 2026-08-07
    ./.venv/bin/python3 scripts/verify_cost_baseline.py --json data/_cache/cost.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402  — loads .env; also works under `railway run`

# Frozen pre-optimization baseline: ai_usage_daily, 2026-07-31..08-04, five
# full days, before PR #129 and #130. $/day averages.
BASELINE_START, BASELINE_END = "2026-07-31", "2026-08-04"
BASELINE = {
    "entity_linker_llm.link_text": 4.153,
    "story_synthesizer.synthesize": 2.366,
    "story_clusterer.cluster": 1.538,
    "summarizer.batch": 0.928,
    "embedder.embed_texts": 0.002,
}
BASELINE_TOTAL = 8.987

# Post-deploy expectations, so the script states a verdict instead of leaving
# the reader to eyeball two columns.
EXPECTED = {
    "entity_linker_llm.link_text": (1.08, "PR #130 regex pre-gate, ~26% forwarded"),
    "story_synthesizer.synthesize": (1.09, "PR #129 reuse skip, 54% of calls were duplicates"),
}


async def _rows(conn, since: str | None, days: int):
    where = "usage_date >= $1::date" if since else \
        f"usage_date > (CURRENT_DATE - INTERVAL '{days} days')"
    args = [since] if since else []
    return await conn.fetch(f"""
        SELECT operation,
               sum(estimated_cost_usd) / count(DISTINCT usage_date) AS per_day,
               sum(call_count)         / count(DISTINCT usage_date) AS calls_per_day,
               count(DISTINCT usage_date) AS days
        FROM ai_usage_daily
        WHERE {where}
        GROUP BY operation
    """, *args)


def _arrow(cur: float, base: float) -> str:
    if base == 0:
        return "     —"
    pct = 100.0 * (cur - base) / base
    return f"{pct:+6.1f}%"


async def main(since: str | None, days: int, out_json: str | None) -> int:
    db_url = settings.database_url
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        rows = await _rows(conn, since, days)
        # Articles/day over the same window — the denominator that turns call
        # counts into the per-article ratio the deploy check reads.
        arts = await conn.fetchval(f"""
            SELECT count(*)::float / GREATEST(count(DISTINCT created_at::date), 1)
            FROM articles
            WHERE created_at > (CURRENT_DATE - INTERVAL '{days} days')
        """)
    finally:
        await conn.close()

    if not rows:
        print("No ai_usage_daily rows in the window. Either the window is wrong, or "
              "usage_tracker._record_to_ledger is short-circuiting again "
              "(it returns early unless ai_cost_guard_enabled is true).")
        return 1

    cur = {r["operation"]: float(r["per_day"] or 0) for r in rows}
    calls = {r["operation"]: float(r["calls_per_day"] or 0) for r in rows}
    window_days = max(int(r["days"]) for r in rows)

    print(f"baseline {BASELINE_START}..{BASELINE_END}   vs   current window "
          f"({window_days} day(s), ~{arts:.0f} articles/day)\n")
    print(f"{'operation':32} {'base $/d':>9} {'now $/d':>9} {'delta':>8}  {'calls/d':>9}")
    total = 0.0
    for op in sorted(set(BASELINE) | set(cur), key=lambda o: -BASELINE.get(o, 0)):
        b, c = BASELINE.get(op, 0.0), cur.get(op, 0.0)
        total += c
        print(f"{op:32} {b:9.2f} {c:9.2f} {_arrow(c, b)}  {calls.get(op, 0):9.0f}")
    print(f"{'TOTAL':32} {BASELINE_TOTAL:9.2f} {total:9.2f} {_arrow(total, BASELINE_TOTAL)}")

    # Deploy check. Ratios, not dollars — a quiet news day also lowers dollars.
    print("\ndeploy check (ratios, so article volume cannot fake a pass):")
    verdicts = {}
    link_ratio = calls.get("entity_linker_llm.link_text", 0) / arts if arts else 0
    ok_gate = link_ratio < 0.6
    verdicts["regex_gate_live"] = ok_gate
    print(f"  linker calls per article    {link_ratio:5.2f}   "
          f"{'PASS — gate live' if ok_gate else 'NOT DEPLOYED (expect ~0.26, ungated ~1.0)'}")

    cl = calls.get("story_clusterer.cluster", 0)
    syn_ratio = calls.get("story_synthesizer.synthesize", 0) / cl if cl else 0
    ok_reuse = 0 < syn_ratio < 3.0
    verdicts["synthesis_reuse_live"] = ok_reuse
    print(f"  synthesize per cluster call {syn_ratio:5.2f}   "
          f"{'PASS — reuse skip live' if ok_reuse else 'NOT DEPLOYED (expect ~2.0, before ~4.4)'}")

    print()
    for op, (target, why) in EXPECTED.items():
        c = cur.get(op, 0.0)
        print(f"  {op:30} {c:6.2f} vs target {target:.2f}  ({why})")

    if out_json:
        os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
        with open(out_json, "w") as fh:
            json.dump({"baseline": BASELINE, "current": cur, "calls_per_day": calls,
                       "articles_per_day": arts, "total": total, "verdicts": verdicts}, fh, indent=2)
        print(f"\nwrote {out_json}")

    # Non-zero when something that should be live is not, so a scheduled run
    # is noisy exactly when it should be.
    return 0 if all(verdicts.values()) else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", help="ISO date; overrides --days")
    p.add_argument("--days", type=int, default=2, help="lookback window (default 2)")
    p.add_argument("--json", dest="out_json", help="also write the report here")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.since, args.days, args.out_json)))
