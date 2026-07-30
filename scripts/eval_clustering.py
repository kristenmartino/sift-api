#!/usr/bin/env python3
"""Evaluate event-level clustering quality against human-labeled ground truth.

Companion to scripts/eval_why_it_matters.py, same shape, different target.

WHY THIS EXISTS
---------------
sift/docs/DECISIONS.md claimed "~97% accuracy on event-level clustering" for
services/story_clusterer.py. There was no eval set, no metric, and no test
behind that number. This is the artifact that replaces the claim.

THREE MODES, THREE COST PROFILES
--------------------------------
  --sample    Read-only DB pull. Emits an UNLABELED corpus for a human to fill
              in. Costs nothing. Run this once, label the file, commit it.

  --replay    Scores committed response fixtures. No API calls, fully
              deterministic, free. This is what CI runs.

  --live      Real Haiku calls against the labeled corpus. ~$0.024 per full
              run at current prices, so cost is not the reason it stays out of
              CI — nondeterminism and the API secret are.

TYPICAL WORKFLOW
----------------
    # 1. generate an unlabeled corpus (needs DATABASE_URL)
    python scripts/eval_clustering.py --sample --batches 6 --per-batch 25

    # 2. label it by hand: fill in "event_id" for each article
    #    (leave null for singletons), mark distractors with "hard": true

    # 3. record fixtures + baseline (costs ~$0.024)
    python scripts/eval_clustering.py --live --repeats 3 --record

    # 4. from then on, CI runs the free deterministic replay
    python scripts/eval_clustering.py --replay
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
from pathlib import Path

# Make `services`/`app` importable when run as a script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services import cluster_metrics  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO / "data" / "eval" / "clustering_corpus.jsonl"
DEFAULT_FIXTURES = REPO / "data" / "eval" / "clustering_responses"
DEFAULT_BASELINE = REPO / "data" / "eval" / "clustering_baseline.json"

# Mirrors ALL_CATEGORIES in scripts/eval_why_it_matters.py. Sports is included
# deliberately: it is where the single-outlet clustering failure concentrates.
SAMPLE_CATEGORIES = ["politics", "world", "technology", "business", "health", "sports"]


# ─── sampling (mode: --sample) ────────────────────────────

SAMPLE_SQL = """
    SELECT id, source_url, source_name, title, summary, entities, published_date
      FROM articles
     WHERE category = $1
       AND from_search = false
       AND summary IS NOT NULL AND summary <> ''
       AND jsonb_typeof(entities) = 'object'
     ORDER BY published_date DESC NULLS LAST
     LIMIT $2
"""


async def sample_corpus(batches: int, per_batch: int, out_path: Path) -> None:
    """Pull real articles and emit an unlabeled corpus for hand-labeling.

    Read-only. Writes nothing to the database.

    Two deliberate choices that protect the eval's validity:

    1. Labels are NOT pre-seeded from the pipeline's own story_id. Seeding
       would cut labeling time ~3x but biases ground truth toward the system
       under test — the standard way eval sets become worthless.
    2. Articles are sorted ALPHABETICALLY BY TITLE within each batch, not by
       published_date and not by story_id, so the ordering carries no signal
       about which articles belong together.

    Privacy: only title, Sift-generated summary, source_name and entities are
    written — no raw article body, no full URL. sift-api/.gitignore excludes
    data/_cache/ precisely to keep publisher text out of git; this keeps the
    committed corpus on the right side of that line while still containing
    everything cluster_articles actually sees.
    """
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    if not db_url:
        raise SystemExit("DATABASE_URL is not set (and settings.database_url is empty)")
    ssl_mode = "require" if "neon.tech" in db_url else False

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)
    try:
        out_batches = []
        for i in range(batches):
            category = SAMPLE_CATEGORIES[i % len(SAMPLE_CATEGORIES)]
            rows = await pool.fetch(SAMPLE_SQL, category, per_batch)
            if not rows:
                print(f"  ! no rows for category={category}, skipping", file=sys.stderr)
                continue

            articles = []
            for r in rows:
                ent = r["entities"]
                if isinstance(ent, str):
                    try:
                        ent = json.loads(ent)
                    except json.JSONDecodeError:
                        ent = {}
                articles.append({
                    "source_name": r["source_name"] or "",
                    "title": r["title"] or "",
                    "summary": r["summary"] or "",
                    "entities": {
                        k: (ent or {}).get(k, [])
                        for k in ("people", "organizations", "locations")
                    },
                    # ── fill these in by hand ──
                    "event_id": None,   # slug shared by same-event articles; null = singleton
                    # "hard": true,     # same TOPIC, different EVENT (distractor)
                    # "distractor_of": "<event_id it is easily confused with>",
                })

            # Alphabetical so ordering leaks nothing about grouping.
            articles.sort(key=lambda a: a["title"].lower())
            for idx, a in enumerate(articles, 1):
                a["idx"] = idx

            out_batches.append({
                "batch_id": f"{category}-{i + 1}",
                "category": category,
                "note": "UNLABELED — set event_id on each article before use",
                "articles": articles,
            })
    finally:
        await pool.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for b in out_batches:
            f.write(json.dumps(b) + "\n")

    total = sum(len(b["articles"]) for b in out_batches)
    print(f"wrote {len(out_batches)} batches / {total} articles -> {out_path}")
    print()
    print("Next: label it. For each article set \"event_id\" to a short slug shared")
    print("by every article covering the SAME SPECIFIC EVENT. Leave it null for")
    print("singletons. Mark same-topic/different-event traps with \"hard\": true.")
    print()
    print("Aim for at least one batch with NO real clusters — nothing currently")
    print("protects against over-clustering a slow news day.")


# ─── corpus loading + prompt hashing ──────────────────────

def load_corpus(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"corpus not found: {path}\nRun with --sample first.")
    batches = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    unlabeled = [b["batch_id"] for b in batches if all(
        a.get("event_id") is None for a in b["articles"]
    )]
    if len(unlabeled) == len(batches):
        raise SystemExit(
            f"every batch in {path} is unlabeled — fill in event_id before scoring.\n"
            "(A corpus where everything is a singleton would score a meaningless ARI.)"
        )
    return batches


def _articles_for_clusterer(batch: dict) -> list[dict]:
    """The exact payload cluster_articles consumes."""
    return [
        {
            "source_url": f"eval://{batch['batch_id']}/{a['idx']}",
            "source_name": a["source_name"],
            "title": a["title"],
            "summary": a["summary"],
            "entities": a.get("entities", {}),
        }
        for a in batch["articles"]
    ]


def prompt_sha256(batch: dict) -> str:
    """Hash the exact prompt cluster_articles would build for this batch.

    Stored in each fixture. The replay test recomputes it and fails loudly on a
    mismatch, so a changed prompt can never be silently scored against a stale
    recorded response — which is what makes replay tests decay into theater.
    """
    from services.story_clusterer import build_prompt

    return hashlib.sha256(build_prompt(_articles_for_clusterer(batch)).encode()).hexdigest()


# ─── scoring ──────────────────────────────────────────────

def score_batch(batch: dict, groups: list[dict]) -> cluster_metrics.ClusteringReport:
    articles = batch["articles"]
    n = len(articles)

    true = cluster_metrics.partition_from_event_ids([a.get("event_id") for a in articles])
    pred = cluster_metrics.partition_from_groups(n, [g["article_indices"] for g in groups])
    outlets = [a["source_name"] for a in articles]

    # Distractor pairs: a "hard" article vs every article of the event it is
    # confusable with.
    hard_pairs: list[tuple[int, int]] = []
    for i, a in enumerate(articles):
        target = a.get("distractor_of")
        if not a.get("hard") or not target:
            continue
        for j, b in enumerate(articles):
            if i != j and b.get("event_id") == target:
                hard_pairs.append((i, j))

    return cluster_metrics.evaluate(true, pred, outlets=outlets, hard_pairs=hard_pairs or None)


def aggregate(reports: list[cluster_metrics.ClusteringReport]) -> dict:
    """Corpus-level numbers. Pairwise metrics are recomputed from raw TP/FP/FN
    (micro-average) rather than averaging per-batch rates, which would let a
    tiny batch swing the result as much as a large one."""
    if not reports:
        return {}
    tp = sum(r.true_positives for r in reports)
    fp = sum(r.false_positives for r in reports)
    fn = sum(r.false_negatives for r in reports)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    def mean(attr: str) -> float:
        vals = [getattr(r, attr) for r in reports if getattr(r, attr) is not None]
        return statistics.fmean(vals) if vals else 0.0

    return {
        "n_batches": len(reports),
        "n_articles": sum(r.n_articles for r in reports),
        "ari": mean("ari"),
        "v_measure": mean("v_measure"),
        "homogeneity": mean("homogeneity"),
        "completeness": mean("completeness"),
        "pairwise_precision": precision,
        "pairwise_recall": recall,
        "pairwise_f1": f1,
        "multi_outlet_precision": mean("multi_outlet_precision"),
        "multi_outlet_recall": mean("multi_outlet_recall"),
        "topic_conflation_rate": mean("topic_conflation_rate"),
        "n_clusters_true": sum(r.n_clusters_true for r in reports),
        "n_clusters_pred": sum(r.n_clusters_pred for r in reports),
    }


# ─── replay (mode: --replay) ──────────────────────────────

def load_fixture(fixtures_dir: Path, batch_id: str) -> dict:
    path = fixtures_dir / f"{batch_id}.json"
    if not path.exists():
        raise SystemExit(
            f"no recorded response for batch {batch_id!r} at {path}\n"
            "Record fixtures first:  python scripts/eval_clustering.py --live --record"
        )
    return json.loads(path.read_text())


def run_replay(corpus: list[dict], fixtures_dir: Path) -> dict:
    from services.story_clusterer import _parse_clusters

    reports = []
    for batch in corpus:
        fixture = load_fixture(fixtures_dir, batch["batch_id"])

        expected = fixture.get("prompt_sha256")
        actual = prompt_sha256(batch)
        if expected and expected != actual:
            raise SystemExit(
                f"Prompt for batch {batch['batch_id']} changed "
                f"(fixture recorded {expected[:12]}…, current is {actual[:12]}…).\n"
                "The recorded response no longer corresponds to the prompt, so "
                "replaying it would measure nothing. Re-record with:\n"
                "  python scripts/eval_clustering.py --live --record"
            )

        groups = _parse_clusters(fixture["response_text"], len(batch["articles"]))
        reports.append(score_batch(batch, groups))
    return aggregate(reports)


# ─── live (mode: --live) ──────────────────────────────────

async def run_live(corpus: list[dict], repeats: int, record_to: Path | None) -> dict:
    import anthropic

    from services.story_clusterer import MODEL, build_prompt, cluster_articles

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    runs: list[dict] = []

    for attempt in range(repeats):
        reports = []
        for batch in corpus:
            articles = _articles_for_clusterer(batch)
            groups = await cluster_articles(articles, client=client)
            reports.append(score_batch(batch, groups))

            if record_to is not None and attempt == 0:
                record_to.mkdir(parents=True, exist_ok=True)
                (record_to / f"{batch['batch_id']}.json").write_text(json.dumps({
                    "batch_id": batch["batch_id"],
                    "model": MODEL,
                    "prompt_sha256": hashlib.sha256(build_prompt(articles).encode()).hexdigest(),
                    "response_text": json.dumps([
                        {
                            "group_id": g["group_id"],
                            "article_indices": g["article_indices"],
                            "event": g["event"],
                        }
                        for g in groups
                    ]),
                }, indent=2) + "\n")

        agg = aggregate(reports)
        runs.append(agg)
        print(f"  run {attempt + 1}/{repeats}: ari={agg['ari']:.3f} f1={agg['pairwise_f1']:.3f}")

    # cluster_articles sets no temperature, so it runs at 1.0. Report the spread
    # and use the MIN when arguing about thresholds.
    out = dict(runs[0])
    if repeats > 1:
        out["metric_spread"] = {
            k: [min(r[k] for r in runs), max(r[k] for r in runs)]
            for k in ("ari", "pairwise_f1", "multi_outlet_precision")
        }
        for k in ("ari", "pairwise_f1"):
            out[k] = statistics.fmean(r[k] for r in runs)
    out["live_repeats"] = repeats
    return out


# ─── reporting ────────────────────────────────────────────

def print_report(agg: dict, baseline: dict | None) -> int:
    if not agg:
        print("no results")
        return 1

    print()
    print(f"  batches {agg['n_batches']}   articles {agg['n_articles']}")
    print(f"  clusters: true {agg['n_clusters_true']}  predicted {agg['n_clusters_pred']}")
    print()
    rows = [
        ("ARI (chance-corrected)", "ari"),
        ("V-measure", "v_measure"),
        ("  homogeneity", "homogeneity"),
        ("  completeness", "completeness"),
        ("pairwise precision", "pairwise_precision"),
        ("pairwise recall", "pairwise_recall"),
        ("pairwise F1", "pairwise_f1"),
        ("multi-outlet precision", "multi_outlet_precision"),
        ("topic conflation rate", "topic_conflation_rate"),
    ]
    for label, key in rows:
        val = agg.get(key)
        if val is None:
            continue
        line = f"  {label:26} {val:.3f}"
        if baseline and key in baseline.get("metrics", {}):
            delta = val - baseline["metrics"][key]
            line += f"   ({delta:+.3f} vs baseline)"
        print(line)

    if spread := agg.get("metric_spread"):
        print()
        print("  spread across repeats (use the MIN when setting thresholds):")
        for k, (lo, hi) in spread.items():
            print(f"    {k:24} {lo:.3f} – {hi:.3f}")

    # Regression gate, only meaningful against a recorded baseline.
    exit_code = 0
    if baseline:
        tol = baseline.get("tolerance", 0.05)
        for key in ("ari", "multi_outlet_precision"):
            base = baseline.get("metrics", {}).get(key)
            cur = agg.get(key)
            if base is not None and cur is not None and cur < base - tol:
                print(f"\n  REGRESSION: {key} {cur:.3f} is more than {tol} below baseline {base:.3f}")
                exit_code = 1
    return exit_code


# ─── entrypoint ───────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sample", action="store_true", help="pull an UNLABELED corpus from the DB (free, read-only)")
    mode.add_argument("--replay", action="store_true", help="score committed fixtures (free, deterministic — what CI runs)")
    mode.add_argument("--live", action="store_true", help="real Haiku calls (~$0.024/run)")

    p.add_argument("--batches", type=int, default=6, help="--sample: number of batches (default 6)")
    p.add_argument("--per-batch", type=int, default=25, help="--sample: articles per batch (default 25)")
    p.add_argument("--repeats", type=int, default=1, help="--live: repeat runs to measure spread")
    p.add_argument("--record", action="store_true", help="--live: (re)write response fixtures + baseline")
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--json", type=Path, help="write the aggregate report to this path")
    args = p.parse_args()

    if args.sample:
        asyncio.run(sample_corpus(args.batches, args.per_batch, args.corpus))
        return

    corpus = load_corpus(args.corpus)
    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None

    if args.replay:
        agg = run_replay(corpus, args.fixtures)
    else:
        agg = asyncio.run(run_live(corpus, args.repeats, args.fixtures if args.record else None))

    exit_code = print_report(agg, baseline)

    if args.json:
        args.json.write_text(json.dumps(agg, indent=2) + "\n")
        print(f"\n  wrote {args.json}")

    if args.live and args.record:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps({
            "model": __import__("services.story_clusterer", fromlist=["MODEL"]).MODEL,
            "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
            "live_repeats": args.repeats,
            # Set from ~2x the observed spread after the first --repeats 5 run.
            "tolerance": 0.05,
            "metrics": {k: v for k, v in agg.items() if isinstance(v, (int, float))},
        }, indent=2) + "\n")
        print(f"  wrote baseline -> {args.baseline}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
