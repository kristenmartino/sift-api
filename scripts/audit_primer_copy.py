"""Read-only replay of primer precision failures and useful-copy controls."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

from app import db
from app.config import settings
from services.cost_estimates import estimate_cost
from services.cost_guard import check_budget
from services.judge import judge_lines
from services.quality_gate import gate_generated_background
from services.usage_tracker import _pending_records


async def main(args):
    settings.ai_cost_guard_enabled = True
    if args.daily_cap is not None:
        settings.daily_ai_cost_limit_usd = args.daily_cap
    db._pool = await asyncpg.create_pool(settings.database_url, ssl="require", min_size=0, max_size=3)
    try:
        rows = json.loads((Path(__file__).parents[1] / "data/eval/primer_precision_regressions.json").read_text())
        actual = {}
        verdicts = {}
        for field in ("background_novelty", "term_utility"):
            candidates = []
            for x in rows:
                if x["field"] != field:
                    continue
                if field == "background_novelty" and not gate_generated_background(x["line"], title=x["title"], summary=x["summary"]):
                    actual[x["id"]] = False
                    verdicts[x["id"]] = {"gate": "withheld"}
                else:
                    candidates.append(x)
            budget = await check_budget(estimate_cost("judge.batch", len(candidates)))
            reviewed = await judge_lines(candidates, field=field) if budget.allowed else []
            by_id = {v["id"]: v for v in reviewed}
            for x in candidates:
                v = by_id.get(x["id"], {})
                verdicts[x["id"]] = v
                actual[x["id"]] = (v.get("restates") is False and v.get("adds_significance") is True and v.get("neutral_cliche_free") is True) if v.get("judged") else None
        failures = [{"id": x["id"], "expected": x["expected"], "actual": actual[x["id"]]} for x in rows if actual[x["id"]] is not x["expected"]]
        print(json.dumps({"checked": len(rows), "failures": failures, "actual": actual, "verdicts": verdicts}, indent=2))
        return bool(failures)
    finally:
        if _pending_records:
            await asyncio.gather(*list(_pending_records), return_exceptions=True)
        await db._pool.close()
        db._pool = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-cap", type=float)
    args = parser.parse_args()
    if args.daily_cap is not None and args.daily_cap <= 0:
        parser.error("daily-cap must be positive")
    raise SystemExit(asyncio.run(main(args)))
