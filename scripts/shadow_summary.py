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
# Above this share of parked articles carrying a near miss, 0.60 is suspect.
NEAR_MISS_CONCERN = 0.35


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
    print(f"near misses: {near_share:.0%} of parked articles have a neighbour in "
          f"[0.50, threshold)")

    print("\nverdicts:")
    ok_parity = live_rate == 0 or shadow_rate >= live_rate * PARITY_FLOOR
    print(f"  grouping parity      {'PASS' if ok_parity else 'FAIL'}  "
          f"{shadow_rate:.1%} vs {live_rate:.1%} live")
    ok_disc = MIN_DISCRIMINATION < reject < (1 - MIN_DISCRIMINATION)
    print(f"  confirmer discriminates {'PASS' if ok_disc else 'CHECK'}  "
          f"{reject:.0%} rejected"
          f"{'' if ok_disc else '  — near 0% is rubber-stamping, near 100% is a broken prompt'}")
    ok_near = near_share <= NEAR_MISS_CONCERN
    print(f"  threshold not too strict {'PASS' if ok_near else 'CHECK'}  "
          f"{near_share:.0%} of parked have a near miss"
          f"{'' if ok_near else f'  — above {NEAR_MISS_CONCERN:.0%}, consider lowering 0.60'}")

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
