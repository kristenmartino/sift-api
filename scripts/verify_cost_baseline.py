"""Compare current Anthropic/Voyage spend against the pre-optimization baseline.

WHY THIS EXISTS
---------------
`STATUS.md:40` carried "~$15/mo" for weeks while the real figure was ~$300/mo,
because `usage_tracker._record_to_ledger` short-circuited unless
`ai_cost_guard_enabled` was true and it defaulted false — so `ai_usage_daily`
was empty and nobody could tell. (#137 decoupled recording from enforcement;
the ledger now fills regardless of that flag.) The fix for a number going
stale is not to write a better number; it is to make re-deriving it a
one-liner.

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

THREE WAYS TO READ THE DOLLARS, AND ONLY ONE IS HONEST
------------------------------------------------------
`raw` compares $/day against the baseline directly. It is wrong on any day
whose volume differs from the baseline's ~1,672 articles/day: on 2026-08-05,
16% busier, it showed `summarizer.batch` at **+17.8%** as if it had regressed,
when volume-adjusted it was **-2.4%** — it simply did 16% more work.

`vol-adj` scales the baseline to the current day's volume first. That is the
column to read.

`$/1k articles` removes volume entirely and is the best single number to
quote, because it stays comparable across days without any scaling assumption.

Targets are stated as a retained *fraction* of baseline spend and scaled by
volume at runtime, not as fixed dollars — a fixed $1.08 linker target was
wrong by 20% on the first day it ran.

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

# Articles/day over the same window. Almost everything here scales with ingest
# volume, so a $/day target that ignores it is wrong on any day that is busier
# or quieter than the baseline — the first real run of this script quoted a
# $1.08 linker target on a day with 16% more articles, where the correct
# number was ~$1.30.
BASELINE_ARTICLES_PER_DAY = 1672

# Operations the baseline never recorded, because the three Batch API handlers
# did not call log_usage until #137. They were always being paid for; only the
# visibility is new. Comparing a total that includes them against BASELINE_TOTAL
# understates the improvement, so the totals are reported both ways.
NEWLY_VISIBLE = {
    "context_generator.batch",
    "primer_generator.batch",
    "entity_extractor.batch",
}

# Post-deploy expectations as a *retained fraction of baseline spend*, scaled at
# runtime by observed volume. Stating them as fractions keeps the assumption
# visible: the linker forwards ~26% of articles (#130), and ~54% of synthesis
# calls were duplicates (#129), so ~46% remains.
EXPECTED_RETAINED = {
    "entity_linker_llm.link_text": (0.26, "PR #130 regex pre-gate, ~26% forwarded"),
    "story_synthesizer.synthesize": (0.46, "PR #129 reuse skip, 54% of calls were duplicates"),
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
              "recording has been gated again — #137 decoupled it from "
              "ai_cost_guard_enabled, and tests/test_usage_tracker.py asserts "
              "usage_tracker imports no settings at all.")
        return 1

    cur = {r["operation"]: float(r["per_day"] or 0) for r in rows}
    calls = {r["operation"]: float(r["calls_per_day"] or 0) for r in rows}
    window_days = max(int(r["days"]) for r in rows)

    # Volume ratio. Everything downstream of ingest scales with it, so a raw
    # $/day delta on a busier day understates the improvement and on a quieter
    # day invents one.
    vol = (arts / BASELINE_ARTICLES_PER_DAY) if arts else 1.0

    print(f"baseline {BASELINE_START}..{BASELINE_END} (~{BASELINE_ARTICLES_PER_DAY:,} articles/day)")
    print(f"current  {window_days} day(s), ~{arts:,.0f} articles/day  "
          f"→ volume {vol:.2f}x baseline\n")

    print(f"{'operation':32} {'base $/d':>9} {'now $/d':>9} {'raw':>7} "
          f"{'vol-adj':>8}  {'$/1k art':>9}")
    total = comparable = 0.0
    for op in sorted(set(BASELINE) | set(cur), key=lambda o: -BASELINE.get(o, 0)):
        b, c = BASELINE.get(op, 0.0), cur.get(op, 0.0)
        total += c
        if op not in NEWLY_VISIBLE:
            comparable += c
        # Scale the baseline up to today's volume before comparing.
        adj = _arrow(c, b * vol) if b else "      —"
        per1k = (c / arts * 1000) if arts else 0.0
        mark = " *" if op in NEWLY_VISIBLE else ""
        print(f"{op:32} {b:9.2f} {c:9.2f} {_arrow(c, b):>7} {adj:>8}  {per1k:9.2f}{mark}")

    base_per1k = BASELINE_TOTAL / BASELINE_ARTICLES_PER_DAY * 1000
    print(f"{'TOTAL (comparable scope)':32} {BASELINE_TOTAL:9.2f} {comparable:9.2f} "
          f"{_arrow(comparable, BASELINE_TOTAL):>7} {_arrow(comparable, BASELINE_TOTAL * vol):>8}  "
          f"{(comparable / arts * 1000) if arts else 0:9.2f}")
    print(f"{'TOTAL (all recorded)':32} {'':>9} {total:9.2f} {'':>7} {'':>8}  "
          f"{(total / arts * 1000) if arts else 0:9.2f}")
    print(f"{'baseline $/1k articles':32} {'':>9} {'':>9} {'':>7} {'':>8}  {base_per1k:9.2f}")
    if any(op in cur for op in NEWLY_VISIBLE):
        print("\n  * Batch API paths the baseline never recorded (#137 added the telemetry).")
        print("    Always paid for; only the visibility is new. Excluded from the")
        print("    comparable-scope total so the baseline is matched like for like.")
    print("\n  vol-adj compares against the baseline scaled to current volume; it is the")
    print("  honest column. $/1k articles is volume-free and the best single number.")

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

    print("\ntargets (baseline x retained-fraction x volume — NOT fixed dollars):")
    for op, (retained, why) in EXPECTED_RETAINED.items():
        c = cur.get(op, 0.0)
        target = BASELINE.get(op, 0.0) * retained * vol
        verdict = "at/under" if c <= target * 1.15 else "above"
        print(f"  {op:30} {c:6.2f} vs {target:5.2f}  {verdict:<9} ({why})")

    if out_json:
        os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
        with open(out_json, "w") as fh:
            json.dump({"baseline": BASELINE, "current": cur, "calls_per_day": calls,
                       "volume_ratio": vol, "comparable_total": comparable,
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
