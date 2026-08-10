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
below, plus a **deploy check**: the changes are visible in call *ratios*, not
just dollars, so the script can tell "not deployed yet" apart from "deployed
and saved nothing" — which a dollars-only diff cannot.

  entity_linker_llm.link_text   ~1 call/article  -> ~0.26 (PR #130 regex gate)
  story_synthesizer.synthesize  ~4.4 per cluster -> ~2.0 (PR #129 reuse skip)

The threading half of that check is **path-aware** as of 2026-08-10, because
incremental threading (#161) retired `story_clusterer.cluster` and a check
divided by a dead operation reports failure at the moment of success. See
`deploy_check`. Exit 3 means the window spans the cutover and no threading
verdict was issued at all.

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
from datetime import date

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


async def _window_start(conn, since: date | None, days: int) -> date:
    """Resolve one start date for BOTH queries below.

    They used to derive their windows independently — the ledger honoured
    `--since` while the article count always used `--days`. Nothing errored;
    the volume denominator just came from a different span than the spend,
    which silently corrupts the vol-adj column and every target computed from
    it. One date, used twice, removes the possibility.
    """
    if since is not None:
        return since
    return await conn.fetchval(
        f"SELECT (CURRENT_DATE - INTERVAL '{days} days')::date + 1"
    )


async def _rows(conn, start: date):
    return await conn.fetch("""
        SELECT operation,
               sum(estimated_cost_usd) / count(DISTINCT usage_date) AS per_day,
               sum(call_count)         / count(DISTINCT usage_date) AS calls_per_day,
               count(DISTINCT usage_date) AS days
        FROM ai_usage_daily
        WHERE usage_date >= $1
        GROUP BY operation
    """, start)


# PR #129 measured ~4.4 synthesize calls per clusterer call before the reuse
# skip and ~2.0 after, so 3.0 separates them on the legacy path.
LEGACY_SYN_PER_CLUSTER_MAX = 3.0

# On the incremental path the denominator is stories *touched*, and synthesis
# fires only on create or on an attach that brings a new outlet
# (`workflows/incremental_threading.py:_attach`). Every touched story costs at
# most one synthesis, so >1.0 sustained means the reuse rule is not holding;
# 2.0 leaves room for a window whose stories were created and then gained a
# new outlet before it closed. Deliberately loose — it exists to catch the
# rule being broken, not to score it.
INCREMENTAL_SYN_PER_STORY_MAX = 2.0


def deploy_check(
    calls: dict[str, float], arts: float, stories_touched: float, threaded: float,
) -> tuple[list[tuple[str, str]], dict[str, bool], str]:
    """Are the cost changes actually running? Returns (lines, verdicts, path).

    Ratios rather than dollars, so a quiet news day cannot fake a pass.

    THE THREADING CHECK HAS TO KNOW WHICH PATH IS LIVE
    --------------------------------------------------
    This check read `synthesize / story_clusterer.cluster` unconditionally. On
    2026-08-10 incremental threading (#161) retired the clusterer, which sends
    that denominator to zero — and `0 < 0 < 3.0` is false, so it would have
    printed **NOT DEPLOYED at the exact moment the cutover fully succeeded**,
    and returned exit 2 with it. It had already started drifting: it read 3.10
    that morning because the clusterer was winding down, not because synthesis
    had regressed.

    WHICH PATH IS LIVE IS NOT A LEDGER QUESTION
    -------------------------------------------
    The obvious repair — treat `story_confirmer.confirm` calls as "incremental
    is live" — is wrong, and prod said so: the 2026-08-08 window reported both
    paths running when only the legacy one was. **Shadow mode bills the
    confirmer too** (20/45/45 calls on 08-07/08/09), and the ledger cannot
    tell a shadow call from a live one, because it is the same operation.

    `articles.threaded_at` can. `story_matcher.mark_threaded` is reached only
    from `run_incremental_threading`, never from the shadow path, so rows
    marked in the window are proof the live path ran. That is the signal used
    here; the confirmer's dollars are reported as shadow spend when they
    appear without it.

    A window with both signals gets **no threading verdict at all** rather
    than a number blended from two systems.
    """
    lines: list[tuple[str, str]] = []
    verdicts: dict[str, bool] = {}

    link_ratio = calls.get("entity_linker_llm.link_text", 0) / arts if arts else 0
    ok_gate = link_ratio < 0.6
    verdicts["regex_gate_live"] = ok_gate
    lines.append((
        f"linker calls per article     {link_ratio:5.2f}",
        "PASS — gate live" if ok_gate else "NOT DEPLOYED (expect ~0.26, ungated ~1.0)",
    ))

    cl = calls.get("story_clusterer.cluster", 0)
    cf = calls.get("story_confirmer.confirm", 0)
    syn = calls.get("story_synthesizer.synthesize", 0)

    if cl and threaded:
        # Daily aggregates cannot separate a window spanning the cutover from
        # a window where both paths ran every cycle. Rather than pick, say
        # both and hand over the one fact that decides it.
        path = "mixed"
        lines.append((
            "threading path                both",
            f"INDETERMINATE — {cl:,.0f} clusterer calls/day alongside "
            f"{threaded:,.0f} articles/day marked by the incremental path. "
            "Either the window spans the cutover, or both paths are billing. "
            "If --since starts after 2026-08-10, it is the latter: two threading "
            "paths running at once, which double-spends and double-assigns.",
        ))
        return lines, verdicts, path

    if threaded:
        path = "incremental"
        ok_retired = cl == 0
        verdicts["legacy_threading_retired"] = ok_retired
        lines.append((
            f"clusterer calls per day      {cl:5.2f}",
            "PASS — legacy path retired" if ok_retired
            else "STILL RUNNING — both paths billing",
        ))
        ratio = syn / stories_touched if stories_touched else 0.0
        ok_reuse = 0 < ratio < INCREMENTAL_SYN_PER_STORY_MAX
        verdicts["synthesis_reuse_live"] = ok_reuse
        lines.append((
            f"synthesize per story touched {ratio:5.2f}",
            "PASS — synthesize-on-change live" if ok_reuse
            else f"CHECK (expect <{INCREMENTAL_SYN_PER_STORY_MAX}; "
                 f"{'no stories touched' if not stories_touched else 'every touch re-synthesizing'})",
        ))
        return lines, verdicts, path

    path = "legacy"
    ratio = syn / cl if cl else 0.0
    ok_reuse = 0 < ratio < LEGACY_SYN_PER_CLUSTER_MAX
    verdicts["synthesis_reuse_live"] = ok_reuse
    lines.append((
        f"synthesize per cluster call  {ratio:5.2f}",
        "PASS — reuse skip live" if ok_reuse
        else "NOT DEPLOYED (expect ~2.0, before ~4.4)",
    ))
    if cf:
        # Confirmer billing with nothing marked threaded: shadow mode. Real
        # money, no product effect — worth naming so it is not mistaken for
        # the cutover having happened.
        lines.append((
            f"confirmer calls per day      {cf:5.2f}",
            "SHADOW — billing while the legacy path is live "
            "(no articles marked threaded in this window)",
        ))
    return lines, verdicts, path


def _arrow(cur: float, base: float) -> str:
    if base == 0:
        return "     —"
    pct = 100.0 * (cur - base) / base
    return f"{pct:+6.1f}%"


async def main(since: date | None, days: int, out_json: str | None) -> int:
    db_url = settings.database_url
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        start = await _window_start(conn, since, days)
        rows = await _rows(conn, start)
        # Articles/day over THE SAME window — the denominator that turns call
        # counts into the per-article ratio the deploy check reads, and the
        # volume ratio every adjusted figure depends on.
        arts = await conn.fetchval("""
            SELECT count(*)::float / GREATEST(count(DISTINCT created_at::date), 1)
            FROM articles
            WHERE created_at::date >= $1
        """, start)
        # Denominator for the incremental path's reuse check. Every write in
        # `incremental_threading` bumps `updated_at`, including the attaches
        # that deliberately skip synthesis — which is the point: it counts
        # chances to synthesize, so the ratio falls as reuse works.
        stories_touched = await conn.fetchval("""
            SELECT count(*)::float / GREATEST(count(DISTINCT updated_at::date), 1)
            FROM stories
            WHERE updated_at::date >= $1
        """, start)
        # Proof the LIVE incremental path ran, which the ledger cannot give:
        # `mark_threaded` is reached only from `run_incremental_threading`,
        # so shadow-mode confirmer calls leave no mark here.
        threaded = await conn.fetchval("""
            SELECT count(*)::float / GREATEST(count(DISTINCT threaded_at::date), 1)
            FROM articles
            WHERE threaded_at::date >= $1
        """, start)
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
    print(f"current  from {start} · {window_days} day(s), ~{arts:,.0f} articles/day  "
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
    lines, verdicts, path = deploy_check(calls, arts, stories_touched, threaded)
    for label, note in lines:
        print(f"  {label}   {note}")

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
                       "articles_per_day": arts, "stories_touched_per_day": stories_touched,
                       "threaded_per_day": threaded,
                       "total": total, "threading_path": path,
                       "verdicts": verdicts}, fh, indent=2)
        print(f"\nwrote {out_json}")

    # Non-zero when something that should be live is not, so a scheduled run
    # is noisy exactly when it should be. A cutover-spanning window is its own
    # code: "I cannot tell" must not be reported as either "fine" or "broken".
    if path == "mixed":
        return 3
    return 0 if all(verdicts.values()) else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="ISO date; overrides --days. Validated at parse time — asyncpg\n"
                        "needs a real date object for a date-typed parameter, and a bare\n"
                        "string raised DataError at query time instead.")
    p.add_argument("--days", type=int, default=2, help="lookback window (default 2)")
    p.add_argument("--json", dest="out_json", help="also write the report here")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.since, args.days, args.out_json)))
