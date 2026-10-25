"""Read-only source-fidelity audit, using the production publication reviewer.

--regressions runs labeled failure/retention cases; otherwise samples recent
production articles and clusters. The existing $15 guard and usage ledger apply.
No timers, updates, regeneration or cache changes. Output preserves error verdicts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import asyncpg

from app import db
from app.config import settings
from services.source_review import assess_copy
from services.usage_tracker import _pending_records


async def main(args):
    settings.ai_cost_guard_enabled = True
    if args.daily_cap is not None:
        settings.daily_ai_cost_limit_usd = args.daily_cap
    db._pool = await asyncpg.create_pool(settings.database_url, min_size=0, max_size=3,
        ssl="require" if "neon.tech" in settings.database_url else None)
    try:
        return await _run_audit(args)
    finally:
        if _pending_records:
            await asyncio.gather(*list(_pending_records), return_exceptions=True)
        await db._pool.close()
        db._pool = None


async def _run_audit(args):
    if args.regressions:
        rows = json.loads((Path(__file__).parents[1] / "data/eval/source_fidelity_regressions.json").read_text())
    else:
        pool = await db.get_pool()
        articles = await pool.fetch("""SELECT id,title,summary,generation_raw_content
            FROM articles WHERE from_search=false AND summary<>''
            ORDER BY created_at DESC LIMIT $1""", args.limit)
        stories = await pool.fetch("""SELECT id,headline,summary FROM stories
            WHERE synthesis_status='complete' ORDER BY updated_at DESC LIMIT $1""", args.limit)
        members = await pool.fetch("""SELECT story_id,title,summary FROM articles
            WHERE story_id=ANY($1::text[]) AND from_search=false""", [s["id"] for s in stories])
        rows = [{"id": a["id"], "title": a["title"], "source": " ".join(re.sub(r"<[^>]+>", "", a["generation_raw_content"] or "").split()[:500]),
                 "copy": a["summary"]} for a in articles if a["generation_raw_content"]]
        rows += [{"id": s["id"], "title": "Supplied cluster reports",
                  "source": "\n".join(a["title"] + "\n" + (a["summary"] or "") for a in members if a["story_id"] == s["id"]),
                  "copy": s["headline"] + "\n" + s["summary"]} for s in stories]
    result = await assess_copy(rows)
    failures = [{"id": r["id"], "expected": r.get("expected", "pass"), "actual": result[r["id"]]}
                for r in rows if result[r["id"]]["v"] != r.get("expected", "pass")]
    print(json.dumps({"checked": len(rows), "failures": failures, "verdicts": result}, indent=2))
    return bool(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regressions", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--daily-cap", type=float, help="Explicit cap for an operator run; default: configured environment cap")
    options = parser.parse_args()
    if options.daily_cap is not None and options.daily_cap <= 0:
        parser.error("daily-cap must be positive")
    if not 1 <= options.limit <= 100:
        parser.error("limit must be between 1 and 100")
    raise SystemExit(asyncio.run(main(options)))
