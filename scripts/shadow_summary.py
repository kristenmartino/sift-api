"""Aggregate incremental-threading shadow runs against the live path.

This is the cutover decision, in one command. Run it after ~24h of shadow
data and read the three verdicts at the bottom.

WHY A SCRIPT AND NOT A QUERY YOU REMEMBER
-----------------------------------------
The comparison is not one number. `would_group` has to be measured per
article and set against what the live rescan path actually groups over the
same window, and two other signals decide whether the answer is trustworthy:
whether the confirmer is discriminating or rubber-stamping, and whether the
0.60 threshold is leaving matches in the near-miss band. Getting that wrong
in either direction — waving through a regression, or blocking a fix that
works — is expensive, and re-deriving it by hand each time is how it stops
being done.

Usage (from sift-api root):

    ./.venv/bin/python3 scripts/shadow_summary.py
    ./.venv/bin/python3 scripts/shadow_summary.py --hours 48
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402

# A cutover must not lose grouping. Slightly under parity is noise on small
# samples; well under is a regression.
PARITY_FLOOR = 0.95
# Below this the confirmer is rubber-stamping rather than judging, and the
# candidate set is doing all the work.
MIN_DISCRIMINATION = 0.05

# How far above its own baseline the near-miss share has to sit before the
# threshold is suspect.
#
# This used to be a bare `NEAR_MISS_CONCERN = 0.35`, which was wrong in a way
# that mattered: measured 2026-08-07, 18.2% of random articles have their top
# in-category neighbour in [0.50, 0.60) and 44.8% are parked, so ~41% of
# parked articles carry a near miss BY CHANCE. A 35% line sits below the
# natural baseline and would have reported CHECK on every run forever — and
# it nearly did talk us into lowering the threshold to 0.50, which the pair
# data says would buy +4.6 points of recall for +18.3 points of pass-through.
#
# The band is roughly 4x enriched in noise: 4.6% of known-true pairs live
# there against 18.2% of random articles. So the question is never "is the
# share high" — it is "is it higher than chance", and chance is a property of
# the embedding distribution that drifts as the corpus changes. Hence a
# baseline computed at runtime rather than a constant that goes stale.
NEAR_MISS_EXCESS = 0.15   # observed - baseline, in absolute share
BASELINE_SAMPLE = 400

# Runs required before any verdict is issued. At a 30-min cadence this is 12
# hours, about 960 sampled articles and ~400 confirmer decisions.
#
# Without this the script cheerfully printed "Cutover bar met" off a single
# 40-article sample — a green light to rewrite the path that produces every
# story a reader sees, on n=1. Each individual verdict is a ratio and looks
# equally confident at any sample size, which is exactly what makes a floor
# necessary rather than advisory.
MIN_RUNS = 24


async def _near_miss_baseline(conn, floor: float, threshold: float) -> float | None:
    """What share of parked articles carry a near miss purely by chance.

    Samples random recent articles, takes each one's best in-category
    neighbour over the same 48h window the matcher uses, and asks: of those
    that would be parked (top < threshold), how many have their top in
    [floor, threshold)?

    That is the null hypothesis the observed share has to beat. Computing it
    rather than hardcoding it means the verdict survives a corpus whose
    embedding distribution shifts — which a constant does not, as the original
    0.35 demonstrated.
    """
    rows = await conn.fetch(
        """
        WITH s AS (
            SELECT id, category, embedding FROM articles
            WHERE created_at > NOW() - INTERVAL '24 hours'
              AND embedding IS NOT NULL AND from_search = false
            ORDER BY random() LIMIT $1
        )
        SELECT (
            SELECT max(1 - (s.embedding <=> b.embedding)) FROM articles b
            WHERE b.category = s.category AND b.id <> s.id AND b.from_search = false
              AND b.published_date > NOW() - INTERVAL '48 hours'
              AND b.embedding IS NOT NULL
        ) AS top FROM s
        """,
        BASELINE_SAMPLE,
    )
    tops = [r["top"] for r in rows if r["top"] is not None]
    parked = [t for t in tops if t < threshold]
    if not parked:
        return None
    return sum(1 for t in parked if t >= floor) / len(parked)


async def main(hours: int) -> int:
    db_url = settings.database_url
    conn = await asyncpg.connect(db_url, ssl="require" if "neon.tech" in db_url else False)
    try:
        agg = await conn.fetchrow(
            f"""
            SELECT count(*) runs,
                   sum(sampled) sampled, sum(llm_relevant) relevant,
                   sum(would_group) would_group,
                   sum(parked) parked, sum(parked_with_near_miss) near,
                   sum(new_cluster_candidates) new_cands,
                   sum(new_clusters_passing_outlet_gate) new_gated,
                   min(run_at) first_run, max(run_at) last_run,
                   count(*) FILTER (WHERE dry_run IS NOT NULL) dry_runs
            FROM threading_shadow
            WHERE run_at > NOW() - INTERVAL '{hours} hours'
            """
        )
        # The live path over the same window, per article, so the two rates
        # are directly comparable.
        live = await conn.fetchrow(
            f"""
            SELECT count(*) arts, count(*) FILTER (WHERE story_id IS NOT NULL) grouped
            FROM articles
            WHERE created_at > NOW() - INTERVAL '{hours} hours'
              AND from_search = false
            """
        )
        cfg = await conn.fetchrow(
            f"""SELECT max(threshold) threshold, max(near_miss_floor) floor
                FROM threading_shadow
                WHERE run_at > NOW() - INTERVAL '{hours} hours'"""
        )
        baseline = None
        if cfg and cfg["threshold"] and cfg["floor"]:
            baseline = await _near_miss_baseline(conn, cfg["floor"], cfg["threshold"])
        actions = await conn.fetchrow(
            f"""
            SELECT coalesce(sum((dry_run->>'attach')::int), 0) attach,
                   coalesce(sum((dry_run->>'new')::int), 0)    new,
                   coalesce(sum((dry_run->>'none')::int), 0)   none
            FROM threading_shadow
            WHERE dry_run IS NOT NULL AND run_at > NOW() - INTERVAL '{hours} hours'
            """
        )
    finally:
        await conn.close()

    if not agg or not agg["runs"]:
        print(f"No threading_shadow rows in the last {hours}h. Either the deploy "
              f"predates migration 018, or incremental_threading_shadow is off.")
        return 1

    print(f"shadow: {agg['runs']} runs ({agg['dry_runs']} with dry run), "
          f"{agg['first_run']:%m-%d %H:%M} .. {agg['last_run']:%m-%d %H:%M}Z\n")

    shadow_rate = (agg["would_group"] or 0) / agg["sampled"] if agg["sampled"] else 0
    live_rate = live["grouped"] / live["arts"] if live["arts"] else 0

    print(f"{'':22} {'articles':>9} {'grouped':>8} {'rate':>7}")
    print(f"{'live rescan path':22} {live['arts']:9,} {live['grouped']:8,} {live_rate:6.1%}")
    print(f"{'incremental (shadow)':22} {agg['sampled']:9,} "
          f"{agg['would_group'] or 0:8,} {shadow_rate:6.1%}")

    if agg["dry_runs"] == 0:
        print("\nNo dry-run data — would_group is unmeasured. Set "
              "INCREMENTAL_THREADING_CONFIRM_DRYRUN=true and wait ~24h.")
        return 1

    total_dec = (actions["attach"] or 0) + (actions["new"] or 0) + (actions["none"] or 0)
    confirmed = (actions["attach"] or 0) + (actions["new"] or 0)
    reject = (actions["none"] or 0) / total_dec if total_dec else 0
    near_share = (agg["near"] or 0) / agg["parked"] if agg["parked"] else 0
    gate_share = (agg["new_gated"] or 0) / agg["new_cands"] if agg["new_cands"] else 0

    print(f"\nconfirmer: {confirmed}/{total_dec} confirmed, {reject:.0%} rejected"
          f"   (attach {actions['attach']}, new {actions['new']}, none {actions['none']})")
    print(f"outlet gate: {gate_share:.0%} of new-cluster candidates carry >=2 outlets")
    if baseline is None:
        print(f"near misses: {near_share:.0%} of parked articles have a neighbour in "
              f"[{cfg['floor']:.2f}, {cfg['threshold']:.2f}) — baseline unavailable")
    else:
        print(f"near misses: {near_share:.0%} of parked articles have a neighbour in "
              f"[{cfg['floor']:.2f}, {cfg['threshold']:.2f})"
              f"   baseline {baseline:.0%} by chance, "
              f"excess {near_share - baseline:+.0%}")

    if agg["runs"] < MIN_RUNS:
        print(f"\nINSUFFICIENT DATA — {agg['runs']} runs, need {MIN_RUNS} "
              f"(~{MIN_RUNS // 2}h at the 30-min cadence).")
        print("Ratios look equally confident at any sample size; that is why "
              "there is a floor. No verdict issued.")
        return 1

    print("\nverdicts:")
    ok_parity = live_rate == 0 or shadow_rate >= live_rate * PARITY_FLOOR
    print(f"  grouping parity      {'PASS' if ok_parity else 'FAIL'}  "
          f"{shadow_rate:.1%} vs {live_rate:.1%} live")
    ok_disc = MIN_DISCRIMINATION < reject < (1 - MIN_DISCRIMINATION)
    print(f"  confirmer discriminates {'PASS' if ok_disc else 'CHECK'}  "
          f"{reject:.0%} rejected"
          f"{'' if ok_disc else '  — near 0% is rubber-stamping, near 100% is a broken prompt'}")
    if baseline is None:
        ok_near = True
        print("  threshold not too strict SKIP  baseline could not be computed")
    else:
        excess = near_share - baseline
        ok_near = excess <= NEAR_MISS_EXCESS
        print(f"  threshold not too strict {'PASS' if ok_near else 'CHECK'}  "
              f"{near_share:.0%} vs {baseline:.0%} expected by chance "
              f"({excess:+.0%})"
              f"{'' if ok_near else f' — more than {NEAR_MISS_EXCESS:.0%} above baseline'}")

    if ok_parity and ok_disc and ok_near:
        print("\n-> Cutover bar met. INCREMENTAL_THREADING_ENABLED=true.")
        return 0
    print("\n-> Bar not met. Do not cut over on this data.")
    return 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=int, default=24)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.hours)))
