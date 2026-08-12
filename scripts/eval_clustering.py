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
    # 1. generate an unlabeled corpus (needs DATABASE_URL).
    #    --per-batch 50 matches the LIMIT in workflows/story_workflow.py, so the
    #    eval measures the same batch size production actually clusters.
    python scripts/eval_clustering.py --sample --batches 6 --per-batch 50

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
import csv
import hashlib
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
import sys
from pathlib import Path

# Make `services`/`app` importable when run as a script from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from services import cluster_metrics  # noqa: E402
from workflows.story_workflow import RECENCY_WINDOW_HOURS  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO / "data" / "eval" / "clustering_corpus.jsonl"
DEFAULT_FIXTURES = REPO / "data" / "eval" / "clustering_responses"
DEFAULT_BASELINE = REPO / "data" / "eval" / "clustering_baseline.json"

# Mirrors ALL_CATEGORIES in scripts/eval_why_it_matters.py. Sports is included
# deliberately: it is where the single-outlet clustering failure concentrates.
SAMPLE_CATEGORIES = ["politics", "world", "technology", "business", "health", "sports"]

# Imported, not redeclared, so the eval's sampling window cannot silently drift
# from the window production actually clusters over.
WINDOW_HOURS = RECENCY_WINDOW_HOURS


# ─── sampling (mode: --sample) ────────────────────────────

# Mirrors the production query in workflows/story_workflow.py:43-57 — same
# filters, same recency window, same recency ordering. The one difference is
# the window OFFSET, which lets each batch sample a different, non-overlapping
# stretch of time.
#
# The window is the whole point. Production only ever asks the clusterer to
# group articles from a single 48h window, because that is the span in which
# two outlets plausibly cover the same event. Sampling "the 25 most recent
# articles" with no window would, in a slow category, reach back days or weeks
# — and articles a week apart are essentially never the same event. That
# corpus would be almost all singletons, and would measure the clusterer on a
# distribution it never actually sees.
SAMPLE_SQL = """
    SELECT id, source_url, source_name, title, summary, entities, published_date
      FROM articles
     WHERE category = $1
       AND from_search = false
       AND summary IS NOT NULL AND summary <> ''
       AND jsonb_typeof(entities) = 'object'
       AND published_date <  NOW() - make_interval(hours => $2)
       AND published_date >= NOW() - make_interval(hours => $3)
     ORDER BY published_date DESC NULLS LAST
     LIMIT $4
"""


async def sample_corpus(
    batches: int,
    per_batch: int,
    out_path: Path,
    window_hours: int = WINDOW_HOURS,
) -> None:
    """Pull real articles and emit an unlabeled corpus for hand-labeling.

    Read-only. Writes nothing to the database.

    SAMPLING FRAME — each batch is one non-overlapping `window_hours` window,
    walking back in time (batch 0 = the most recent window, batch 1 = the one
    before it, ...), paired with a different category. This mirrors
    workflows/story_workflow.py, which only ever hands the clusterer a single
    48h window. Two consequences that matter:

      * Cluster density matches production. Sampling "the N most recent
        articles" with no window would, in a slow category, span days — and
        articles days apart are essentially never the same event, so the
        corpus would be nearly all singletons and would measure the clusterer
        on a distribution it never sees.
      * Batches are genuinely distinct. Without a per-batch window offset,
        asking for more batches than categories would re-fetch the SAME
        articles under a different batch_id.

    It is deliberately NOT a uniform random sample of all articles. A random
    draw across months would contain almost no same-event pairs, which is the
    only thing this eval measures. Within a window, articles are taken by
    recency exactly as production does.

    Three further choices that protect validity:

    1. Labels are NOT pre-seeded from the pipeline's own story_id. Seeding
       would cut labeling time ~3x but biases ground truth toward the system
       under test — the standard way eval sets become worthless.
    2. Articles are sorted ALPHABETICALLY BY TITLE within each batch, not by
       published_date and not by story_id, so the ordering carries no signal
       about which articles belong together.
    3. Slow windows are kept, not discarded. A window with no real clusters is
       the control case: every other metric punishes under-clustering, and
       nothing punishes inventing groups on a quiet news day.

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
        # (batch_id, n_articles, n_outlets, top_outlet_share, top_outlet, window)
        diagnostics: list[tuple[str, int, int, float, str, str]] = []
        for i in range(batches):
            category = SAMPLE_CATEGORIES[i % len(SAMPLE_CATEGORIES)]
            # Batch i covers [now - (i+1)*window, now - i*window).
            start_h = i * window_hours
            end_h = (i + 1) * window_hours
            rows = await pool.fetch(SAMPLE_SQL, category, start_h, end_h, per_batch)
            if not rows:
                print(
                    f"  ! {category}: no articles in the window "
                    f"{end_h}h–{start_h}h ago, skipping",
                    file=sys.stderr,
                )
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

            dates = [r["published_date"] for r in rows if r["published_date"]]
            span = (
                f"{min(dates):%Y-%m-%d %H:%M} .. {max(dates):%Y-%m-%d %H:%M}"
                if dates else "unknown"
            )
            outlet_counts = Counter(r["source_name"] for r in rows if r["source_name"])
            n_outlets = len(outlet_counts)
            top_outlet, top_n = (
                outlet_counts.most_common(1)[0] if outlet_counts else ("", 0)
            )
            top_share = top_n / len(rows) if rows else 0.0

            out_batches.append({
                "batch_id": f"{category}-{i + 1}",
                "category": category,
                "window_hours": window_hours,
                "window_span": span,
                "n_outlets": n_outlets,
                "top_outlet_share": round(top_share, 2),
                "note": "UNLABELED — set event_id on each article before use",
                "articles": articles,
            })
            diagnostics.append((
                f"{category}-{i + 1}",
                len(articles),
                n_outlets,
                top_share,
                top_outlet,
                span,
            ))
    finally:
        await pool.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for b in out_batches:
            f.write(json.dumps(b) + "\n")

    csv_path = write_labels_csv(out_batches, out_path)

    total = sum(len(b["articles"]) for b in out_batches)
    print(f"wrote {len(out_batches)} batches / {total} articles")
    print(f"  corpus (machine):  {out_path}")
    print(f"  labels (edit me):  {csv_path}")
    print()

    # Sanity-check the sample BEFORE anyone spends hours labeling it.
    print(f"  {'batch':16} {'arts':>4} {'outlets':>7} {'top %':>9}  window")
    for bid, n, n_out, share, top, span in diagnostics:
        flag = ""
        if n < 8:
            flag = "  <- THIN: too small to contain clusters"
        elif n_out < 2:
            flag = "  <- SINGLE OUTLET: cannot produce a story"
        elif share > 0.4:
            flag = f"  <- SKEWED: {top}"
        print(f"  {bid:16} {n:>4} {n_out:>7} {share:>9.0%}  {span}{flag}")
    print()

    thin = [b for b, n, *_ in diagnostics if n < 8]
    single = [b for b, _, n_out, *_ in diagnostics if n_out < 2]
    skewed = [(b, top, share) for b, _, _, share, top, _ in diagnostics if share > 0.4]

    if thin:
        print(f"  WARNING: too thin to contain clusters: {thin}")
        print("  Raise --per-batch or widen --window-hours, then re-sample.")
    if single:
        print(f"  WARNING: single-outlet batch(es): {single}")
        print("  Cross-outlet coverage is what a story IS — these cannot produce one.")
    if skewed:
        print("  NOTE: one outlet dominates these batches —")
        for b, top, share in skewed:
            print(f"    {b}: {share:.0%} {top}")
        print("  Not necessarily bad: a skewed batch is the adversarial case for the")
        print("  >=2-unique-outlets gate (the '4x same-outlet posts rendered as *how 4")
        print("  outlets covered this*' failure). Keep ONE on purpose; more than that")
        print("  and the corpus is mostly measuring the outlet gate, not clustering.")
    if not (thin or single or skewed):
        print("  Sample looks usable: enough articles, and no single outlet dominates.")
    print()
    print("Next: label it. For each article set \"event_id\" to a short slug shared")
    print("by every article covering the SAME SPECIFIC EVENT. Leave it null for")
    print("singletons. Mark same-topic/different-event traps with \"hard\": true.")
    print()
    print("Aim for at least one batch with NO real clusters — nothing currently")
    print("protects against over-clustering a slow news day.")


# ─── annotator provenance ─────────────────────────────────

# Recorded INSIDE every labeled batch, not only in prose, so the corpus cannot
# be quoted as human-labeled by someone who never read the docs. Whoever reports
# a number off this corpus has to say what produced the ground truth.
ANNOTATION_PROVENANCE = {
    "annotator": "claude-opus-5",
    "method": "machine",
    "date": "2026-07-30",
    "basis": "title + Sift-generated summary only",
    "not_used": ["story_id", "embedding", "entities"],
    "caveat": (
        "MACHINE-ANNOTATED. The clusterer under test is also an LLM (Haiku), so any "
        "score against this corpus measures LLM-vs-LLM agreement, not human-validated "
        "accuracy. Report it as such. Run --review-sample / --agreement to attach a "
        "human spot-check and a Cohen's kappa before quoting the number anywhere."
    ),
}


# ─── human spot-check (modes: --review-sample / --agreement) ──

def review_sample(corpus_path: Path, out_path: Path, n_pairs: int = 40) -> None:
    """Emit a small set of article PAIRS for a human to adjudicate independently.

    Pairs, not articles: a binary same-event/different-event call on 40 pairs is
    perhaps 15 minutes, where re-labeling 50 articles is an hour — and pairs are
    exactly the unit the pairwise metrics are computed over.

    Negatives are NOT sampled at random. A random pair of articles is trivially
    "different" and would inflate agreement toward 1.0 while measuring nothing.
    They are drawn from the highest lexical-similarity pairs the machine labeled
    as different — i.e. the genuinely confusable ones, where an annotator
    disagreement is informative.
    """
    batches = [
        json.loads(line) for line in corpus_path.read_text().splitlines() if line.strip()
    ]
    positives, negatives = [], []
    for b in batches:
        arts = b["articles"]
        n = len(arts)
        words = {a["idx"]: _content_words(a["title"]) for a in arts}
        df = Counter(w for ws in words.values() for w in ws)
        ceiling = max(2, n // 4)
        for i, a in enumerate(arts):
            for c in arts[i + 1:]:
                same = bool(a.get("event_id")) and a.get("event_id") == c.get("event_id")
                shared = {w for w in words[a["idx"]] & words[c["idx"]] if 2 <= df[w] <= ceiling}
                score = sum(math.log(n / df[w]) for w in shared) if shared else 0.0
                rec = (score, b["batch_id"], a, c)
                (positives if same else negatives).append(rec)

    if not positives:
        raise SystemExit("corpus has no labeled clusters — nothing to review")

    # Evenly spaced across the positives so one huge cluster cannot dominate.
    step = max(1, len(positives) // (n_pairs // 2))
    pos = positives[::step][: n_pairs // 2]
    negatives.sort(key=lambda r: -r[0])
    neg = negatives[: n_pairs - len(pos)]

    # Shuffle deterministically. Emitting positives first then negatives leaks
    # the answer: a reviewer who notices the boundary has every remaining
    # judgment contaminated. Ordering by a hash of the pair identity is
    # reproducible (same corpus -> same order, so a review can be re-generated
    # and compared) while being uncorrelated with the label.
    rows = sorted(
        pos + neg,
        key=lambda r: hashlib.sha256(
            f"{r[1]}:{r[2]['idx']}:{r[3]['idx']}".encode()
        ).hexdigest(),
    )
    with out_path.open("w", newline="", encoding=CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow([
            "pair_id", "batch_id", "idx_a", "source_a", "title_a",
            "idx_b", "source_b", "title_b", "your_verdict",
        ])
        for i, (_score, bid, a, c) in enumerate(rows, 1):
            w.writerow([
                i, bid, a["idx"], a["source_name"], a["title"],
                c["idx"], c["source_name"], c["title"], "",
            ])

    print(f"wrote {len(rows)} pairs -> {out_path}\n")
    print("For each pair put `same` or `different` in your_verdict — do these two")
    print("articles cover the SAME specific event? The machine's answer is not shown,")
    print("deliberately: seeing it would anchor you and destroy the measurement.")
    print("\nNegatives are the most lexically similar non-matches, not random pairs,")
    print("so this is a hard test rather than a flattering one.")
    print(f"\nThen:  python scripts/eval_clustering.py --agreement {out_path}")


def agreement(corpus_path: Path, review_path: Path) -> None:
    """Compare a human's pair verdicts to the machine labels; report Cohen's kappa."""
    batches = [
        json.loads(line) for line in corpus_path.read_text().splitlines() if line.strip()
    ]
    idx = {
        (b["batch_id"], a["idx"]): a.get("event_id")
        for b in batches for a in b["articles"]
    }
    with review_path.open(newline="", encoding=CSV_ENCODING) as f:
        rows = list(csv.DictReader(f))

    both_same = both_diff = human_same_only = machine_same_only = 0
    disagreements = []
    scored = 0
    for r in rows:
        v = (r.get("your_verdict") or "").strip().lower()
        if v not in {"same", "different", "s", "d"}:
            continue
        scored += 1
        human = v.startswith("s")
        ea = idx.get((r["batch_id"], int(r["idx_a"])))
        eb = idx.get((r["batch_id"], int(r["idx_b"])))
        machine = bool(ea) and ea == eb
        if human and machine:
            both_same += 1
        elif not human and not machine:
            both_diff += 1
        elif human:
            human_same_only += 1
            disagreements.append(("human=same machine=different", r))
        else:
            machine_same_only += 1
            disagreements.append(("human=different machine=same", r))

    if not scored:
        raise SystemExit(
            f"no verdicts found in {review_path} — fill in the your_verdict column "
            "with `same` or `different`"
        )

    n = scored
    po = (both_same + both_diff) / n
    # Cohen's kappa: agreement corrected for what chance alone would produce.
    p_h = (both_same + human_same_only) / n
    p_m = (both_same + machine_same_only) / n
    pe = p_h * p_m + (1 - p_h) * (1 - p_m)
    kappa = 1.0 if pe == 1 else (po - pe) / (1 - pe)

    print(f"  pairs adjudicated:      {n}")
    print(f"  raw agreement:          {po:.1%}")
    print(f"  Cohen's kappa:          {kappa:.3f}")
    print()
    print(f"  both said same:         {both_same}")
    print(f"  both said different:    {both_diff}")
    print(f"  human same / machine different: {human_same_only}")
    print(f"  human different / machine same: {machine_same_only}")

    if kappa >= 0.80:
        verdict = "strong — machine labels are a reasonable stand-in for human ground truth"
    elif kappa >= 0.60:
        verdict = "moderate — usable, but report the kappa alongside any accuracy number"
    else:
        verdict = ("weak — do NOT quote accuracy off this corpus without relabeling "
                   "the disputed cases by hand")
    print(f"\n  {verdict}")

    if disagreements:
        print(f"\n  disagreements ({len(disagreements)}) — these are the cases worth "
              "resolving by hand:")
        for kind, r in disagreements[:10]:
            print(f"    [{kind}]  {r['batch_id']}  {r['idx_a']} ~ {r['idx_b']}")
            print(f"      {r['title_a'][:66]}")
            print(f"      {r['title_b'][:66]}")


# ─── labeling aid (mode: --candidates) ────────────────────

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]{2,}")

# Ordinary English function words. Not a topic stoplist — deliberately no
# political or news vocabulary here, because "which words are too generic" is a
# judgment that belongs to the person labeling, not baked into the tool.
_STOPWORDS = set("""
the this that what how why when where who new after before his her its their not but and for with from
into over under here there these those said says could would should will can has have had was were are
is be been being out off down up all any more most other some such only own same than too very one two
you your they them then now about against between during through above below over into while because
""".split())


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)} - _STOPWORDS


def _find(parent: dict[int, int], x: int) -> int:
    """Union-find root with path compression. Module-level so it does not close
    over a loop variable (ruff B023)."""
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def show_candidates(corpus_path: Path, threshold: float) -> None:
    """Surface likely-related articles so labeling is scanning, not hunting.

    WHAT THIS IS NOT: it is not the clusterer's output, and it does not use
    embeddings, `entities`, or `story_id`. Using any of those to build ground
    truth would make the eval circular — you would be measuring whether the
    system agrees with itself. This is plain lexical overlap on TITLE text
    (publisher-written, the most neutral signal available), scored by inverse
    document frequency within a batch.

    Sorting the CSV alphabetically already puts same-event articles adjacent
    when their titles share a leading word ("Seattle ..."), but misses clusters
    whose titles start differently — the Blanche nomination articles begin
    "Senate", "Steve" and "Trump" and are scattered across the batch.

    Everything printed is a CANDIDATE. Some are real clusters, some are
    same-topic/different-event pairs — the second kind are exactly what should
    be marked `hard` + `distractor_of`. The call is yours on every one.
    """
    if not corpus_path.exists():
        raise SystemExit(f"corpus not found: {corpus_path}")
    batches = [
        json.loads(line)
        for line in corpus_path.read_text().splitlines()
        if line.strip()
    ]

    for b in batches:
        articles = b["articles"]
        n = len(articles)
        words = {a["idx"]: _content_words(a["title"]) for a in articles}
        df = Counter(w for ws in words.values() for w in ws)
        by_idx = {a["idx"]: a for a in articles}

        # Ignore words appearing in more than a quarter of the batch: within one
        # category "senate"/"trump" carry almost no information about WHICH
        # event an article covers.
        ceiling = max(2, n // 4)
        scored = []
        for i, a in enumerate(articles):
            for c in articles[i + 1:]:
                shared = {
                    w for w in words[a["idx"]] & words[c["idx"]]
                    if 2 <= df[w] <= ceiling
                }
                if not shared:
                    continue
                score = sum(math.log(n / df[w]) for w in shared)
                scored.append((score, a["idx"], c["idx"], shared))
        scored.sort(reverse=True)

        # Union-find over above-threshold pairs, so a 3-article event shows as
        # one group rather than three separate pairs.
        parent: dict[int, int] = {}
        terms: dict[tuple[int, int], set[str]] = {}
        for score, x, y, shared in scored:
            if score < threshold:
                continue
            terms[(x, y)] = shared
            parent[_find(parent, x)] = _find(parent, y)

        grouped: dict[int, list[int]] = defaultdict(list)
        for idx in list(parent):
            grouped[_find(parent, idx)].append(idx)
        groups = {k: sorted(v) for k, v in grouped.items() if len(v) >= 2}

        print(f"\n{'=' * 78}\n{b['batch_id']}  —  {n} articles, "
              f"{len(groups)} candidate group(s) at score >= {threshold}\n{'=' * 78}")
        if not groups:
            print("  (none — either a genuinely quiet window, or lower --threshold)")

        for gi, members in enumerate(sorted(groups.values(), key=len, reverse=True), 1):
            linking = sorted(
                {w for (x, y), s in terms.items() if x in members and y in members for w in s},
                key=lambda w: df[w],
            )
            outlets = {by_idx[m]["source_name"] for m in members}
            flag = "" if len(outlets) >= 2 else "   [single outlet — cannot be a story]"
            print(f"\n  candidate {gi}: {len(members)} articles, "
                  f"{len(outlets)} outlet(s){flag}")
            print(f"    linked by: {', '.join(linking[:8])}")
            for m in members:
                a = by_idx[m]
                print(f"      idx {m:>3}  [{a['source_name'][:16]:16}] {a['title'][:62]}")

        # Pairs just below the line: the richest source of `hard` distractors,
        # because "shares vocabulary but is a different event" is exactly what
        # topic_conflation_rate measures.
        near = [p for p in scored if threshold * 0.6 <= p[0] < threshold][:5]
        if near:
            print("\n  borderline — check these for same-topic/different-event traps:")
            for score, x, y, shared in near:
                print(f"    {score:5.1f}  idx {x} ~ {y}  ({', '.join(sorted(shared)[:3])})")
                print(f"           {by_idx[x]['title'][:64]}")
                print(f"           {by_idx[y]['title'][:64]}")

    print(f"\n{'=' * 78}")
    print("These are CANDIDATES from title-word overlap only — not the clusterer's")
    print("output, and not embeddings. Real clusters and same-topic traps both show")
    print("up here; telling them apart is the judgment the eval is built to capture.")
    print("Put a shared slug in event_id for real ones; mark traps hard=yes.")


# ─── label CSV export (mode: --export-labels) ─────────────

LABEL_COLUMNS = [
    "batch_id", "idx", "source_name", "title", "summary",
    "event_id", "hard", "distractor_of",
]

# Excel — especially on macOS — reads a plain UTF-8 CSV as the system legacy
# encoding and mangles anything non-ASCII. Article titles are full of curly
# quotes, em-dashes and accented names, so the BOM that "utf-8-sig" prepends is
# what makes the file open correctly by double-click. Python's csv reader
# strips the BOM transparently when the file is read back with the same codec.
CSV_ENCODING = "utf-8-sig"


def write_labels_csv(batches: list[dict], corpus_path: Path) -> Path:
    """Write the flat, hand-editable labeling CSV for `batches`.

    The corpus JSONL is the machine format: one batch per line, which at
    --per-batch 50 is a single ~25,000-character line. Hand-editing that to
    find each "event_id": null is miserable and error-prone. This is the
    interface a grouping task actually wants — one row per article, sortable,
    so you can sort by title, see the clusters line up, and type a slug.
    """
    csv_path = corpus_path.with_suffix(".labels.csv")
    with csv_path.open("w", newline="", encoding=CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow(LABEL_COLUMNS)
        for b in batches:
            for a in b["articles"]:
                w.writerow([
                    b["batch_id"], a["idx"], a["source_name"], a["title"],
                    a["summary"],
                    a.get("event_id") or "",
                    "yes" if a.get("hard") else "",
                    a.get("distractor_of") or "",
                ])
    return csv_path


def export_labels(corpus_path: Path) -> None:
    """Regenerate the labeling CSV from an existing corpus.

    Exists because the corpus and its CSV can drift apart: --sample writes both,
    but a corpus generated before the CSV feature existed (or one whose CSV was
    deleted) would otherwise need a full re-sample to get one — and re-sampling
    produces a DIFFERENT set of articles, since the windows walk back from the
    current time. This regenerates the CSV in place, preserving the sample.

    Any labels already present in the corpus are carried into the CSV, so this
    is also the safe way to resume a partially-labeled corpus.
    """
    if not corpus_path.exists():
        raise SystemExit(
            f"corpus not found: {corpus_path}\n"
            "Generate one first:  python scripts/eval_clustering.py --sample"
        )
    batches = [
        json.loads(line)
        for line in corpus_path.read_text().splitlines()
        if line.strip()
    ]
    csv_path = write_labels_csv(batches, corpus_path)

    n_articles = sum(len(b["articles"]) for b in batches)
    n_labeled = sum(
        1 for b in batches for a in b["articles"] if a.get("event_id")
    )
    print(f"wrote {n_articles} rows -> {csv_path}")
    if n_labeled:
        print(f"  ({n_labeled} existing labels carried over)")
    print()
    print("Open it in Excel, Numbers or Sheets — NOT the .jsonl, which is JSON")
    print("Lines (one object per line) and is not a single JSON document, so")
    print("Power Query reports \"extra characters at the end of the JSON input\".")
    print()
    print("Sort by title within a batch: same-event articles land next to each")
    print("other. Put a shared slug in event_id, leave singletons blank, and mark")
    print("same-topic/different-event traps with hard=yes + distractor_of.")
    print()
    print(f"Then:  python scripts/eval_clustering.py --ingest-labels {csv_path}")


# ─── label ingest (mode: --ingest-labels) ─────────────────

def ingest_labels(corpus_path: Path, csv_path: Path) -> None:
    """Merge a hand-labeled CSV back into the corpus JSONL, then validate.

    Validation runs BEFORE anything is written and before any paid --live call,
    because a mislabeled corpus produces confident, wrong numbers — the worst
    possible failure for an eval.
    """
    batches = [
        json.loads(line)
        for line in corpus_path.read_text().splitlines()
        if line.strip()
    ]
    by_id = {b["batch_id"]: b for b in batches}

    with csv_path.open(newline="", encoding=CSV_ENCODING) as f:
        rows = list(csv.DictReader(f))

    errors: list[str] = []
    applied = 0
    for r in rows:
        bid, idx = r["batch_id"], r["idx"]
        batch = by_id.get(bid)
        if batch is None:
            errors.append(f"unknown batch_id {bid!r}")
            continue
        art = next((a for a in batch["articles"] if str(a["idx"]) == str(idx)), None)
        if art is None:
            errors.append(f"{bid}: no article with idx {idx}")
            continue

        event_id = (r.get("event_id") or "").strip() or None
        hard = (r.get("hard") or "").strip().lower() in {"1", "true", "yes", "y", "x"}
        distractor_of = (r.get("distractor_of") or "").strip() or None

        art["event_id"] = event_id
        art.pop("hard", None)
        art.pop("distractor_of", None)
        if hard:
            art["hard"] = True
            art["distractor_of"] = distractor_of
        applied += 1

    # ── semantic checks ──
    for b in batches:
        events = {a["event_id"] for a in b["articles"] if a.get("event_id")}
        for a in b["articles"]:
            if not a.get("hard"):
                continue
            tgt = a.get("distractor_of")
            if not tgt:
                errors.append(
                    f"{b['batch_id']}#{a['idx']}: marked hard but distractor_of is empty "
                    "— a distractor must name the event it is confusable with"
                )
            elif tgt not in events:
                errors.append(
                    f"{b['batch_id']}#{a['idx']}: distractor_of={tgt!r} is not an "
                    f"event_id in this batch (have: {sorted(events)})"
                )
            elif a.get("event_id") == tgt:
                errors.append(
                    f"{b['batch_id']}#{a['idx']}: is labeled as event {tgt!r} AND as a "
                    "distractor of it — contradictory; a distractor is a DIFFERENT event"
                )

    if errors:
        print("LABEL ERRORS — nothing was written:\n", file=sys.stderr)
        for e in errors[:40]:
            print(f"  {e}", file=sys.stderr)
        if len(errors) > 40:
            print(f"  ... and {len(errors) - 40} more", file=sys.stderr)
        raise SystemExit(1)

    with corpus_path.open("w") as f:
        for b in batches:
            b["note"] = "labeled"
            b["annotation"] = ANNOTATION_PROVENANCE
            f.write(json.dumps(b) + "\n")

    # ── is this corpus actually worth running? ──
    print(f"applied {applied} labels -> {corpus_path}\n")
    print(f"  {'batch':16} {'arts':>4} {'clusters':>8} {'largest':>7} {'pairs':>6} {'x-outlet':>8}")
    total_pairs = 0
    total_clusters = 0
    total_cross = 0
    all_singleton_batches = 0
    for b in batches:
        groups: dict[str, list[dict]] = {}
        for a in b["articles"]:
            if a.get("event_id"):
                groups.setdefault(a["event_id"], []).append(a)
        real = {k: v for k, v in groups.items() if len(v) >= 2}
        pairs = sum(len(v) * (len(v) - 1) // 2 for v in real.values())
        cross = sum(
            1 for v in real.values()
            if len({a["source_name"] for a in v}) >= 2
        )
        largest = max((len(v) for v in real.values()), default=0)
        total_pairs += pairs
        total_clusters += len(real)
        total_cross += cross
        if not real:
            all_singleton_batches += 1
        print(
            f"  {b['batch_id']:16} {len(b['articles']):>4} {len(real):>8} "
            f"{largest:>7} {pairs:>6} {cross:>8}"
        )

    n_hard = sum(1 for b in batches for a in b["articles"] if a.get("hard"))
    n_arts = sum(len(b["articles"]) for b in batches)
    print(f"\n  {total_clusters} clusters, {total_pairs} positive pairs out of "
          f"{n_arts * (n_arts - 1) // 2} possible, {n_hard} distractors marked")

    if total_pairs < 20:
        print("\n  WARNING: fewer than 20 positive pairs. Pairwise recall will be very")
        print("  noisy — each mistake moves it several percent. Consider labeling more")
        print("  batches before recording a baseline you intend to gate on.")
    if total_clusters and total_cross == 0:
        print("\n  WARNING: no cross-outlet clusters. multi_outlet_precision measures "
              "what users actually see, and will be meaningless without them.")
    if n_hard == 0:
        print("\n  NOTE: no distractors marked. topic_conflation_rate will report 0.0 "
              "and measure nothing — it is the metric for the prompt's central claim.")
    if all_singleton_batches == 0:
        print("\n  NOTE: every batch has at least one cluster. Consider labeling one "
              "batch as all-singletons — nothing else here punishes over-clustering.")


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
    mode.add_argument("--ingest-labels", type=Path, metavar="CSV",
                      help="merge a hand-labeled CSV back into the corpus, then validate")
    mode.add_argument("--export-labels", action="store_true",
                      help="regenerate the labeling CSV from an existing corpus (no DB, no re-sample)")
    mode.add_argument("--candidates", action="store_true",
                      help="suggest likely-related articles to speed up labeling (lexical only)")
    mode.add_argument("--review-sample", type=Path, metavar="CSV",
                      help="emit article PAIRS for an independent human spot-check")
    mode.add_argument("--agreement", type=Path, metavar="CSV",
                      help="score a completed review CSV against the corpus (Cohen's kappa)")

    p.add_argument("--batches", type=int, default=6, help="--sample: number of batches (default 6)")
    p.add_argument("--per-batch", type=int, default=25, help="--sample: articles per batch (default 25)")
    p.add_argument(
        "--window-hours", type=int, default=WINDOW_HOURS,
        help=f"--sample: hours per batch window (default {WINDOW_HOURS}, matching production)",
    )
    p.add_argument("--review-pairs", type=int, default=40,
                   help="--review-sample: how many pairs to emit (default 40)")
    p.add_argument(
        "--threshold", type=float, default=4.5,
        help="--candidates: IDF score cutoff (default 4.5; lower surfaces more, noisier)",
    )
    p.add_argument("--repeats", type=int, default=1, help="--live: repeat runs to measure spread")
    p.add_argument("--record", action="store_true", help="--live: (re)write response fixtures + baseline")
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--json", type=Path, help="write the aggregate report to this path")
    args = p.parse_args()

    if args.review_sample:
        review_sample(args.corpus, args.review_sample, args.review_pairs)
        return

    if args.agreement:
        agreement(args.corpus, args.agreement)
        return

    if args.candidates:
        show_candidates(args.corpus, args.threshold)
        return

    if args.export_labels:
        export_labels(args.corpus)
        return

    if args.ingest_labels:
        ingest_labels(args.corpus, args.ingest_labels)
        return

    if args.sample:
        asyncio.run(
            sample_corpus(args.batches, args.per_batch, args.corpus, args.window_hours)
        )
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
