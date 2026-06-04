"""Cheap re-gate of already-stored card copy (sift-api#90 cleanup).

`ON CONFLICT DO UPDATE` in the pipeline does NOT re-run generation, so the
~20%-restatement / ~17%-cliché lines the sift#150 audit found persist in prod
until something reprocesses them. This script is that something — the cheap way.

It runs ONLY the deterministic quality gate (services/quality_gate.py) over
stored rows and:
  - NULLs why_it_matters lines that fail (restatement / cliché / empty), and
  - blanks context_primer.background paragraphs that hit a cliché — KEEPING the
    glossary `terms` (the differentiated value) untouched.

No regeneration, so it's near-free (no model calls unless you pass --judge).
A full regenerate-with-new-rubric pass is a separate, paid step (re-run the
pipeline / backfill_context.py); this just clears the bad lines now.

Defaults to a DRY RUN — prints what would change. Pass --apply to write.

Examples:
  ./.venv/bin/python3 scripts/regate_existing.py                  # dry run, both fields
  ./.venv/bin/python3 scripts/regate_existing.py --apply          # write the drops
  ./.venv/bin/python3 scripts/regate_existing.py --field why_it_matters --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services import judge as judge_mod  # noqa: E402
from services.quality_gate import evaluate_background, evaluate_why_it_matters  # noqa: E402


async def _connect() -> asyncpg.Pool:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    return await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)


async def regate_why_it_matters(pool: asyncpg.Pool, args) -> None:
    rows = await pool.fetch(
        """
        SELECT source_url, title, summary, why_it_matters
          FROM articles
         WHERE why_it_matters IS NOT NULL AND why_it_matters <> ''
         ORDER BY published_date DESC NULLS LAST
         LIMIT $1
        """,
        args.limit,
    )
    print(f"\n[why_it_matters] scanning {len(rows)} stored lines...")

    drops: list[tuple[str, str, str]] = []  # (source_url, reason, line)
    for r in rows:
        res = evaluate_why_it_matters(r["why_it_matters"], title=r["title"] or "", summary=r["summary"] or "")
        if res.dropped:
            drops.append((r["source_url"], res.reason, r["why_it_matters"]))

    det_drop_urls = {u for u, _, _ in drops}

    # Optional: also drop judge-failed survivors (restates / not neutral).
    if args.judge:
        survivors = [r for r in rows if r["source_url"] not in det_drop_urls]
        verdicts = await judge_mod.judge_lines(
            [{"id": r["source_url"], "title": r["title"] or "", "summary": r["summary"] or "",
              "line": r["why_it_matters"]} for r in survivors],
            field="why_it_matters",
        )
        for v in verdicts:
            if v.get("judged") and v["verdict"] == "fail":
                drops.append((v["id"], f"judge:{v.get('reason', 'fail')}", v["line"]))

    by_reason: dict[str, int] = {}
    for _, reason, _ in drops:
        key = reason.split(":")[0]
        by_reason[key] = by_reason.get(key, 0) + 1
    print(f"  would drop {len(drops)}/{len(rows)} ({(len(drops) / len(rows) * 100) if rows else 0:.0f}%)"
          f"  by reason={by_reason or '{}'}")
    for u, reason, line in drops[:8]:
        print(f"    - [{reason}] {line[:80]}")

    if args.apply and drops:
        urls = list({u for u, _, _ in drops})
        await pool.execute(
            "UPDATE articles SET why_it_matters = NULL, updated_at = NOW() "
            "WHERE source_url = ANY($1::text[])",
            urls,
        )
        print(f"  APPLIED: NULLed why_it_matters on {len(urls)} rows.")
    elif drops:
        print("  (dry run — pass --apply to write)")


async def regate_background(pool: asyncpg.Pool, args) -> None:
    rows = await pool.fetch(
        """
        SELECT source_url, title, summary, context_primer->>'background' AS background
          FROM articles
         WHERE context_primer->>'background' IS NOT NULL
           AND context_primer->>'background' <> ''
         ORDER BY published_date DESC NULLS LAST
         LIMIT $1
        """,
        args.limit,
    )
    print(f"\n[background] scanning {len(rows)} stored paragraphs...")

    drops: list[tuple[str, str]] = []  # (source_url, background)
    for r in rows:
        res = evaluate_background(r["background"], title=r["title"] or "", summary=r["summary"] or "")
        if res.dropped:
            drops.append((r["source_url"], r["background"]))

    print(f"  would blank {len(drops)}/{len(rows)} ({(len(drops) / len(rows) * 100) if rows else 0:.0f}%)"
          f" backgrounds (terms kept)")
    for u, bg in drops[:8]:
        print(f"    - {bg[:90]}")

    if args.apply and drops:
        # Blank the background in place; terms + generated_at are preserved.
        await pool.executemany(
            "UPDATE articles "
            "SET context_primer = jsonb_set(context_primer, '{background}', '\"\"'::jsonb), "
            "    updated_at = NOW() "
            "WHERE source_url = $1",
            [(u,) for u, _ in drops],
        )
        print(f"  APPLIED: blanked background on {len(drops)} rows (terms intact).")
    elif drops:
        print("  (dry run — pass --apply to write)")


async def run(args) -> None:
    pool = await _connect()
    try:
        if args.field in ("both", "why_it_matters"):
            await regate_why_it_matters(pool, args)
        if args.field in ("both", "background"):
            await regate_background(pool, args)
    finally:
        await pool.close()
    if not args.apply:
        print("\nDry run complete. Re-run with --apply to write the drops.")


def main() -> None:
    p = argparse.ArgumentParser(description="Re-gate stored why_it_matters / background (sift-api#90)")
    p.add_argument("--field", choices=["both", "why_it_matters", "background"], default="both")
    p.add_argument("--limit", type=int, default=100000, help="max rows per field to scan")
    p.add_argument("--judge", action="store_true",
                   help="also drop judge-failed survivors (costs API spend)")
    p.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
