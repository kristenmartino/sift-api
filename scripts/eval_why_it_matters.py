"""LLM-judge eval for the why_it_matters / background quality gate (sift-api#90).

Baselines AI-generated card copy against the sift#150 audit and proves the
generation rubric + quality gate moved the restatement + cliché rates.

What it reports, per run:
  - Lexical-novelty buckets  → the audit-comparable column (sift#150's proxy).
  - Deterministic gate       → % dropped, by reason (cliche/restatement/empty).
  - LLM judge (with --judge) → restatement / cliché / adds-significance / pass
                               rates over the lines that would actually SHOW.

Modes:
  baseline   score lines as they are (existing prod copy) — reproduces the audit.
  candidate  regenerate lines with the new rubric+gate, then score the survivors.
  compare    run both over the SAME rows and print the before/after delta.

Sources:
  (default)  the committed fixture corpus (data/eval/why_it_matters_corpus.jsonl)
             — deterministic, no network unless --judge.
  --from-db  pull live rows from the database (read-only SELECT).

Examples:
  # cheap, offline, deterministic gate only (what CI-style smoke runs)
  ./.venv/bin/python3 scripts/eval_why_it_matters.py

  # the full prod before/after the issue asks for (judge over Sonnet)
  ./.venv/bin/python3 scripts/eval_why_it_matters.py --from-db --limit 500 \
      --judge --mode compare --json eval_out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services import judge as judge_mod  # noqa: E402
from services.context_generator import generate_context  # noqa: E402
from services.primer_generator import generate_primers  # noqa: E402
from services.quality_gate import evaluate_background, evaluate_why_it_matters, lexical_novelty  # noqa: E402

DEFAULT_CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "eval", "why_it_matters_corpus.jsonl",
)

ALL_CATEGORIES = [
    "top", "technology", "business", "science", "energy",
    "world", "health", "politics", "sports", "entertainment",
]

NOVELTY_BUCKETS = [
    ("0-25%   (pure restatement)", 0.00, 0.25),
    ("25-50%  (mostly restatement)", 0.25, 0.50),
    ("50-75%  (adds a real angle)", 0.50, 0.75),
    ("75-100% (largely new)", 0.75, 1.01),
]


# --- sources ---------------------------------------------------------------

def load_corpus(path: str, field: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("field", "why_it_matters") != field:
                continue
            rows.append({
                "key": r.get("id"),
                "title": r.get("title", ""),
                "summary": r.get("summary", ""),
                "line": r.get("line", ""),
            })
    return rows


async def load_from_db(limit: int, field: str, stratify: bool = True) -> list[dict]:
    """Pull live rows (read-only). Stratified across the 10 feed categories by
    default so the sample matches the sift#150 audit ("across all 10
    categories") rather than a recency-biased slice — recent ingest skews
    heavily by category, which would distort the rates. Excludes from_search
    rows so it mirrors the real feed."""
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)

    line_expr = "context_primer->>'background'" if field == "background" else "why_it_matters"
    where = (
        f"{line_expr} IS NOT NULL AND {line_expr} <> '' "
        "AND summary IS NOT NULL AND summary <> '' AND from_search = false"
    )
    try:
        db_rows: list = []
        if stratify:
            per_cat = max(1, -(-limit // len(ALL_CATEGORIES)))  # ceil div
            for cat in ALL_CATEGORIES:
                db_rows += await pool.fetch(
                    f"SELECT source_url, title, summary, category, {line_expr} AS line "
                    f"FROM articles WHERE {where} AND category = $1 "
                    "ORDER BY published_date DESC NULLS LAST LIMIT $2",
                    cat, per_cat,
                )
        else:
            db_rows = await pool.fetch(
                f"SELECT source_url, title, summary, category, {line_expr} AS line "
                f"FROM articles WHERE {where} "
                "ORDER BY published_date DESC NULLS LAST LIMIT $1",
                limit,
            )
    finally:
        await pool.close()
    return [
        {"key": r["source_url"], "title": r["title"] or "", "summary": r["summary"] or "",
         "category": r["category"], "line": r["line"] or ""}
        for r in db_rows
    ][:limit]


# --- regeneration (candidate) ---------------------------------------------

async def regenerate(rows: list[dict], field: str) -> dict[str, str | None]:
    """Re-run generation with the new rubric+gate; return key -> gated line."""
    articles = [
        {"source_url": r["key"], "title": r["title"], "summary": r["summary"],
         "source_name": "eval"}
        for r in rows
    ]
    if field == "background":
        results = await generate_primers(articles)
        return {k: (v.get("background") or "") for k, v in results.items()}
    results = await generate_context(articles)
    return {k: v.get("context") for k, v in results.items()}


# --- scoring ---------------------------------------------------------------

def gate_of(field: str):
    return evaluate_background if field == "background" else evaluate_why_it_matters


async def score(rows: list[dict], field: str, run_judge: bool) -> dict:
    """Score a set of {key,title,summary,line} rows. Empty lines count as
    suppressed (no card line shown); judge + novelty run over shown lines only."""
    gate = gate_of(field)
    shown = [r for r in rows if (r["line"] or "").strip()]
    suppressed = len(rows) - len(shown)

    # Lexical-novelty buckets (audit-comparable), over shown lines.
    bucket_counts = [0] * len(NOVELTY_BUCKETS)
    for r in shown:
        nov = lexical_novelty(r["line"], f"{r['title']} {r['summary']}")
        for i, (_, lo, hi) in enumerate(NOVELTY_BUCKETS):
            if lo <= nov < hi:
                bucket_counts[i] += 1
                break

    # Deterministic gate, over shown lines.
    gate_reasons: dict[str, int] = {}
    gate_dropped = 0
    for r in shown:
        res = gate(r["line"], title=r["title"], summary=r["summary"])
        if res.dropped:
            gate_dropped += 1
            gate_reasons[res.reason] = gate_reasons.get(res.reason, 0) + 1

    # Suppression by category — surfaces where null-over-filler bites hardest
    # (the sports/entertainment civic-stake question, #71).
    by_category: dict[str, dict] = {}
    for r in rows:
        cat = r.get("category") or "?"
        d = by_category.setdefault(cat, {"rows": 0, "shown": 0})
        d["rows"] += 1
        if (r["line"] or "").strip():
            d["shown"] += 1

    report = {
        "n_rows": len(rows),
        "shown": len(shown),
        "suppressed": suppressed,
        "novelty_buckets": {NOVELTY_BUCKETS[i][0]: bucket_counts[i] for i in range(len(NOVELTY_BUCKETS))},
        "gate_dropped": gate_dropped,
        "gate_dropped_pct": (gate_dropped / len(shown)) if shown else 0.0,
        "gate_reasons": gate_reasons,
        "by_category": by_category,
    }

    if run_judge and shown:
        verdicts = await judge_mod.judge_lines(
            [{"id": r["key"], "title": r["title"], "summary": r["summary"], "line": r["line"]}
             for r in shown],
            field=field,
        )
        report["judge"] = judge_mod.tally(verdicts)
    return report


# --- printing --------------------------------------------------------------

def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def print_report(title: str, rep: dict, field: str) -> None:
    print(f"\n=== {title} ===")
    print(f"rows={rep['n_rows']}  shown={rep['shown']}  suppressed={rep['suppressed']}"
          f" ({_fmt_pct(rep['suppressed'] / rep['n_rows']) if rep['n_rows'] else '0%'})")
    print("  lexical novelty vs title+summary (audit-comparable):")
    shown = rep["shown"] or 1
    for label, count in rep["novelty_buckets"].items():
        print(f"    {label:32} {count:5d}  {_fmt_pct(count / shown)}")
    print(f"  deterministic gate would drop: {rep['gate_dropped']} "
          f"({_fmt_pct(rep['gate_dropped_pct'])})  reasons={rep['gate_reasons'] or '{}'}")
    if "judge" in rep:
        j = rep["judge"]
        print(f"  LLM judge (n={j['n']}, errors={j['errors']}):")
        print(f"    restatement rate        {_fmt_pct(j['restatement_rate'])}")
        print(f"    cliché/editorial rate   {_fmt_pct(j['cliche_or_editorial_rate'])}")
        print(f"    adds-significance rate  {_fmt_pct(j['adds_significance_rate'])}")
        print(f"    PASS rate               {_fmt_pct(j['pass_rate'])}")
    cats = {c: d for c, d in rep.get("by_category", {}).items() if c != "?"}
    if cats:
        print("  shown by category (line kept / rows):")
        for cat in sorted(cats, key=lambda c: cats[c]["shown"] / max(1, cats[c]["rows"])):
            d = cats[cat]
            print(f"    {cat:14} {d['shown']:3d}/{d['rows']:<3d}  {_fmt_pct(d['shown'] / max(1, d['rows']))} shown")


def print_delta(before: dict, after: dict) -> None:
    print("\n=== BEFORE → AFTER (the measurable drop) ===")
    b_supp = before["suppressed"] / before["n_rows"] if before["n_rows"] else 0
    a_supp = after["suppressed"] / after["n_rows"] if after["n_rows"] else 0
    print(f"  suppressed (no line shown):  {_fmt_pct(b_supp)} → {_fmt_pct(a_supp)}")
    print(f"  deterministic-gate drops:    {_fmt_pct(before['gate_dropped_pct'])} → {_fmt_pct(after['gate_dropped_pct'])}")
    if "judge" in before and "judge" in after:
        jb, ja = before["judge"], after["judge"]
        for label, key in [("restatement", "restatement_rate"),
                           ("cliché/editorial", "cliche_or_editorial_rate"),
                           ("adds-significance", "adds_significance_rate"),
                           ("PASS", "pass_rate")]:
            print(f"  judge {label:18} {_fmt_pct(jb[key])} → {_fmt_pct(ja[key])}")


# --- main ------------------------------------------------------------------

async def run(args) -> None:
    if args.from_db:
        rows = await load_from_db(args.limit, args.field)
        print(f"Pulled {len(rows)} rows from DB (field={args.field}).")
    else:
        rows = load_corpus(args.corpus, args.field)
        print(f"Loaded {len(rows)} rows from corpus (field={args.field}).")

    if not rows:
        print("No rows to evaluate.")
        return

    out: dict = {"field": args.field, "mode": args.mode, "source": "db" if args.from_db else "corpus"}

    if args.mode in ("baseline", "compare"):
        baseline = await score(rows, args.field, args.judge)
        print_report("BASELINE (existing lines)", baseline, args.field)
        out["baseline"] = baseline

    if args.mode in ("candidate", "compare"):
        regen = await regenerate(rows, args.field)
        cand_rows = [{**r, "line": regen.get(r["key"]) or ""} for r in rows]
        candidate = await score(cand_rows, args.field, args.judge)
        print_report("CANDIDATE (new rubric + gate)", candidate, args.field)
        out["candidate"] = candidate

    if args.mode == "compare":
        print_delta(out["baseline"], out["candidate"])

    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nWrote {args.json}")


def main() -> None:
    p = argparse.ArgumentParser(description="LLM-judge eval for why_it_matters / background (sift-api#90)")
    p.add_argument("--from-db", action="store_true", help="pull live rows instead of the fixture corpus")
    p.add_argument("--limit", type=int, default=500, help="max rows when --from-db (default 500)")
    p.add_argument("--field", choices=["why_it_matters", "background"], default="why_it_matters")
    p.add_argument("--mode", choices=["baseline", "candidate", "compare"], default="baseline")
    p.add_argument("--judge", action="store_true", help="run the LLM judge (costs API spend)")
    p.add_argument("--corpus", default=DEFAULT_CORPUS, help="path to the fixture JSONL")
    p.add_argument("--json", help="write the full report to this JSON path")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
