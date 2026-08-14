#!/usr/bin/env python3
"""Measure how much the summarizer disagrees with ITSELF, before comparing it to anything.

WHY THIS EXISTS
---------------
`summarizer.batch` is the highest-volume LLM call in the pipeline and the
second-largest line on the bill, and it has no eval at all — only mocked unit
tests and alignment instrumentation. `scripts/project_model_cost.py` says a
budget-tier model would cut it from ~$26/mo to ~$2/mo, which makes it the first
stage worth evaluating properly.

But a candidate cannot be compared to the incumbent until the incumbent's own
run-to-run noise is known. `docs/SOURCE_SCALING.md` records why, at length: the
linker's batching experiment nearly shipped the opposite conclusion because
batch-vs-single was measured without ever measuring single-vs-single. The
single-article path agrees with itself 97.3%, which is what made the 15-18
point gap real rather than noise.

The clustering eval then produced the other half of that lesson on 2026-08-13:
its metrics spread 0.11-0.29 across repeats of identical input, wide enough
that a naive tolerance could not distinguish a regression from a redraw. There
is no reason to assume the summarizer is more stable than clustering or less
stable than the linker. Measure it.

WHAT THIS MEASURES
------------------
Category is a closed 10-way set, so agreement is unambiguous and needs no judge
and no labels: run the incumbent N times over identical input and count how
often it picks the same label. That number decides whether a category A/B is
feasible at all, and at what n.

Summary text is scored only for gross stability (length, lexical overlap).
Judging prose quality needs a cross-vendor judge panel and bias controls, which
is a later piece of work — this deliberately does not pretend to do it.

WHY THE CORPUS COMES FROM A LIVE FETCH
--------------------------------------
`raw_content` is never persisted: it exists on the in-flight `RSSArticle` and
`store_node` writes the SUMMARY, not the input. So there is no way to replay
historical summarizer inputs, and the corpus has to be captured from a live RSS
fetch. That is free and uses the real production reader.

Consequence worth stating: this corpus is one moment's news, so its category
mix is whatever was breaking that hour. Sample size and stratification matter
more here than for a corpus drawn across weeks.

Usage:
    # 1. capture a corpus from live feeds (free, no LLM)
    ./.venv/bin/python3 scripts/eval_summarizer.py --sample --n 150

    # 2. measure the incumbent against itself (~$0.42 at n=150, repeats=5)
    ./.venv/bin/python3 scripts/eval_summarizer.py --self-agreement --repeats 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import RSSArticle  # noqa: E402
from services import summarizer, usage_tracker  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO / "data" / "eval" / "summarizer_corpus.jsonl"

# What _build_prompt itself applies. Storing more than the model sees would
# bloat the corpus and misrepresent the input.
PROMPT_WORD_LIMIT = 500


# ─── corpus (mode: --sample) ──────────────────────────────

async def sample_corpus(n: int, out: Path) -> None:
    from services.rss import fetch_feeds

    articles = await fetch_feeds()
    print(f"  fetched {len(articles)} articles from live feeds")

    # Same gate production applies: a headline with no body cannot be
    # summarized, and asking anyway is how apologies ended up on cards (#118).
    usable = [a for a in articles if summarizer._has_summarizable_content(a)]
    print(f"  {len(usable)} carry summarizable body text")

    # Deduplicate by source_url. Outlets with section feeds (NPR / NPR World /
    # NPR Health, The Hill / The Hill Politics) publish the same URL to both,
    # so a raw fetch carries the same article twice — measured at 22 of 581,
    # 3.79%, on 2026-08-13. Results are keyed by source_url, so leaving them in
    # silently scores those articles once while counting them twice in the
    # denominator: the first version of this script reported "150 scored, 0
    # dropped" against runs that had summarized 147.
    #
    # This is sift-api#145 seen from the corpus side. Production pays for both
    # copies through every per-article stage and collapses them only at store
    # time, via ON CONFLICT (source_url).
    seen: set[str] = set()
    deduped = []
    for a in usable:
        if a.source_url not in seen:
            seen.add(a.source_url)
            deduped.append(a)
    if len(deduped) < len(usable):
        n_dup = len(usable) - len(deduped)
        print(
            f"  dropped {n_dup} duplicate source_url(s) "
            f"({n_dup / len(usable):.2%} of the fetch) — see sift-api#145"
        )
    usable = deduped

    # Spread across outlets rather than taking the first N, which would be
    # dominated by whichever feed returned most. docs/SOURCE_SCALING.md: the
    # top 10 sources are 64% of volume, so an unstratified draw measures Sports
    # Illustrated and the New York Post.
    by_source: dict[str, list[RSSArticle]] = defaultdict(list)
    for a in usable:
        by_source[a.source_name].append(a)

    picked: list[RSSArticle] = []
    round_no = 0
    while len(picked) < n:
        added = False
        for source in sorted(by_source):
            if round_no < len(by_source[source]) and len(picked) < n:
                picked.append(by_source[source][round_no])
                added = True
        if not added:
            break
        round_no += 1

    from services.rss import stable_hash

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for a in picked:
            f.write(json.dumps({
                # Same id function production uses for article ids. Also a
                # second line of defence on the duplicate-url problem above:
                # tests/test_meta_suite.py rejects a corpus with duplicate
                # ids, so a dupe that slipped the sampler fails in CI rather
                # than quietly inflating a denominator.
                "id": stable_hash(a.source_url),
                "source_url": a.source_url,
                "title": a.title,
                "source_name": a.source_name,
                # Truncated to exactly what the model sees.
                "raw_content": summarizer._truncate(a.raw_content, PROMPT_WORD_LIMIT),
            }) + "\n")

    print(
        f"\n  wrote {len(picked)} articles across "
        f"{len({a.source_name for a in picked})} outlets -> {out}"
    )
    print("  Commit it: the whole point is that every run scores the same input.")


def load_corpus(path: Path) -> list[RSSArticle]:
    if not path.exists():
        raise SystemExit(
            f"No corpus at {path}. Capture one first:\n"
            f"  ./.venv/bin/python3 scripts/eval_summarizer.py --sample --n 150"
        )
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            # `id` is corpus bookkeeping, not part of the production model.
            d.pop("id", None)
            out.append(RSSArticle(**d))
    return out


# ─── running the incumbent ────────────────────────────────

class _LocalLedger:
    """Capture spend instead of writing it to the production ledger.

    `summarize_articles` calls `log_usage`, which posts to `ai_usage_daily`
    under the operation id `summarizer.batch` — the same row
    `scripts/project_model_cost.py` reads. An eval run left unguarded would
    inject its own spend into the numbers used to decide the eval, and a
    15-repeat run is thousands of calls' worth of contamination in a table
    quoted as production cost.

    (It happens to be near-harmless today only by accident: `_record_to_ledger`
    fires and forgets onto the event loop, so a short script often exits before
    the write lands. Depending on that is not a plan.)
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._original = None

    def __enter__(self) -> _LocalLedger:
        self._original = usage_tracker._record_to_ledger
        usage_tracker._record_to_ledger = self._capture
        return self

    def __exit__(self, *exc) -> None:
        usage_tracker._record_to_ledger = self._original

    def _capture(self, operation, model, cost_usd, call_count=1, **tokens) -> None:
        self.calls.append({
            "operation": operation, "model": model,
            "cost_usd": cost_usd, "call_count": call_count, **tokens,
        })

    @property
    def total_usd(self) -> float:
        return sum(c["cost_usd"] for c in self.calls)


async def run_once(corpus: list[RSSArticle]) -> dict[str, dict]:
    """One full pass through the PRODUCTION path — prompt, parse, retry, gate."""
    return await summarizer.summarize_articles(corpus)


# ─── scoring ──────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    return {w for w in "".join(
        c.lower() if c.isalnum() or c.isspace() else " " for c in text
    ).split() if len(w) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


def score(runs: list[dict[str, dict]], corpus: list[RSSArticle]) -> dict:
    # dict.fromkeys, not set(), so the order is stable AND a duplicated
    # source_url counts once. Results are keyed by url, so a duplicate scores
    # the same article twice and inflates the denominator — which is how the
    # first run of this script reported 150 scored against 147 summarized.
    # The sampler dedupes too; this stays because a corpus can be hand-edited.
    urls = list(dict.fromkeys(a.source_url for a in corpus))
    # Only articles every run produced, so a run that dropped one does not
    # silently change the denominator between metrics.
    common = [u for u in urls if all(u in r for r in runs)]

    pair_agree, unanimous = [], 0
    disagreements: Counter = Counter()
    for u in common:
        cats = [r[u]["category"] for r in runs]
        pairs = list(combinations(cats, 2))
        pair_agree.append(sum(x == y for x, y in pairs) / len(pairs) if pairs else 1.0)
        if len(set(cats)) == 1:
            unanimous += 1
        else:
            disagreements[tuple(sorted(set(cats)))] += 1

    sims, lengths = [], []
    for u in common:
        summaries = [r[u]["summary"] for r in runs]
        pairs = list(combinations(summaries, 2))
        if pairs:
            sims.append(statistics.fmean(jaccard(x, y) for x, y in pairs))
        lengths.extend(len(s.split()) for s in summaries)

    return {
        "n_corpus_rows": len(corpus),
        "n_corpus": len(urls),
        "n_scored": len(common),
        "n_dropped": len(urls) - len(common),
        "category_pairwise_agreement": statistics.fmean(pair_agree) if pair_agree else 0.0,
        "category_unanimous_rate": unanimous / len(common) if common else 0.0,
        "summary_jaccard": statistics.fmean(sims) if sims else 0.0,
        "summary_words_mean": statistics.fmean(lengths) if lengths else 0.0,
        "top_disagreements": disagreements.most_common(8),
        "category_mix": Counter(runs[0][u]["category"] for u in common).most_common(),
    }


def report(s: dict, repeats: int, ledger: _LocalLedger) -> None:
    dupes = s["n_corpus_rows"] - s["n_corpus"]
    dup_note = f" ({dupes} duplicate url{'s' if dupes != 1 else ''})" if dupes else ""
    print(f"\n  corpus {s['n_corpus']}{dup_note}   scored {s['n_scored']}   "
          f"dropped {s['n_dropped']}   repeats {repeats}")
    print()
    print(f"  category pairwise agreement   {s['category_pairwise_agreement']:.3f}")
    print(f"  category unanimous across all {s['category_unanimous_rate']:.3f}")
    print(f"  summary lexical overlap       {s['summary_jaccard']:.3f}")
    print(f"  summary length (words, mean)  {s['summary_words_mean']:.1f}")

    if s["top_disagreements"]:
        print("\n  where it disagrees with itself:")
        for cats, count in s["top_disagreements"]:
            print(f"    {count:4d}  {' / '.join(cats)}")

    print("\n  category mix (run 1):")
    for cat, count in s["category_mix"]:
        print(f"    {cat:16s} {count:4d}")

    agree = s["category_pairwise_agreement"]
    print(f"\n  eval spend: ${ledger.total_usd:.4f} over {len(ledger.calls)} logged calls")
    print(
        "\n  HOW TO READ THIS. The number that matters is category pairwise\n"
        "  agreement: it is the floor any candidate comparison sits on. A\n"
        "  candidate that agrees with the incumbent LESS than the incumbent\n"
        "  agrees with itself has told you nothing."
    )
    disagree = 1 - agree
    print(
        f"    self-disagreement here is {disagree:.1%}, so a candidate scoring\n"
        f"    within ~{disagree:.1%} of the incumbent is indistinguishable from a redraw."
    )
    if agree >= 0.95:
        print("    >= 0.95: stable enough that a category A/B is feasible at modest n.")
    elif agree >= 0.85:
        print("    0.85-0.95: usable, but only large effects will be conclusive.")
    else:
        print(
            "    < 0.85: the incumbent is too unstable on this metric for a\n"
            "    category A/B to decide anything at reasonable n. Either raise\n"
            "    repeats substantially, or judge summaries directly instead."
        )

    stable = s["category_unanimous_rate"]
    print(
        f"\n  AND THE BETTER TEST. Self-disagreement is not spread evenly — it\n"
        f"  concentrates on genuinely ambiguous articles (business/technology,\n"
        f"  entertainment/politics above are boundary calls, not randomness).\n"
        f"  {stable:.1%} of articles get the SAME label in all {repeats} runs.\n"
        f"  Score a candidate on that stable subset: there the incumbent has no\n"
        f"  noise by construction, so every disagreement is signal. Scoring the\n"
        f"  full corpus mixes real differences with coin-flips on articles the\n"
        f"  incumbent cannot decide either, and buries a small effect."
    )
    print(
        "\n  Consistency is not accuracy. A model can be perfectly stable and\n"
        "  stably wrong, and nothing here reads the labels — that needs gold or\n"
        "  adjudication. This bounds what a comparison can detect, not whether\n"
        "  the incumbent is any good."
    )


async def self_agreement(corpus: list[RSSArticle], repeats: int, out_json: Path | None) -> int:
    runs = []
    with _LocalLedger() as ledger:
        for i in range(repeats):
            result = await run_once(corpus)
            runs.append(result)
            cats = Counter(v["category"] for v in result.values())
            print(f"  run {i + 1}/{repeats}: {len(result)} summarized, "
                  f"{len(cats)} categories used")

    s = score(runs, corpus)
    report(s, repeats, ledger)

    if out_json:
        out_json.write_text(json.dumps(s, indent=2, default=str) + "\n")
        print(f"\n  wrote {out_json}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sample", action="store_true",
                      help="capture a corpus from live RSS (free)")
    mode.add_argument("--self-agreement", action="store_true",
                      help="run the incumbent N times over the corpus and score agreement")
    p.add_argument("--n", type=int, default=150, help="--sample: articles to capture")
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--json", dest="out_json", type=Path)
    a = p.parse_args()

    if a.sample:
        asyncio.run(sample_corpus(a.n, a.corpus))
        return
    sys.exit(asyncio.run(self_agreement(load_corpus(a.corpus), a.repeats, a.out_json)))


if __name__ == "__main__":
    main()
