#!/usr/bin/env python3
"""Does the faithfulness judge actually discriminate? Measure before trusting.

WHY THIS EXISTS
---------------
`scripts/eval_summary_quality.py` was built, run, and its result retracted. It
reported the INCUMBENT at 0.288 "supported" — 71% of Sift's live summaries
supposedly carrying unsupported claims — which was measuring RSS teasers rather
than the model.

The identity control it did have returned exactly 1.000: the judge agreed with
itself on every one of 25 items. That reads as reassuring and is not. A judge
that answers "unsupported" to everything also agrees with itself perfectly.
**Consistency and discrimination are different properties, and only one of them
was measured.**

So this measures the other one, which the original plan specified and the first
attempt skipped:

  SENSITIVITY — plant a violation the judge MUST catch, and see if it does.
    A judge that misses planted corruptions cannot be trusted to find real
    ones, and its pass rate means nothing.

  SPECIFICITY — run the same judge on unmodified summaries and see how often
    it objects anyway. This is what the retracted 0.288 was actually made of.

Both are needed. Sensitivity alone would pass a judge that flags everything;
specificity alone would pass one that flags nothing.

HOW THE VIOLATIONS ARE PLANTED
------------------------------
Deterministically, not by asking a model to corrupt text — that would make the
ground truth as uncertain as the thing being measured. Each corruption is a
mechanical edit whose violation is true by construction:

  supported   append a sentence containing a fabricated figure, after first
              confirming that figure appears nowhere in the source article
  legal_safe  rewrite hedged legal language into an assertion of fact
              ("charged with" -> "guilty of", "alleged" -> "confirmed")
  attributed  strip the attribution clause the summary carried

The legal and attribution edits only apply where the pattern exists, so those
sample sizes are smaller and are reported separately rather than pooled.

Usage:
    ./.venv/bin/python3 scripts/eval_judge_calibration.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.eval_summarizer import DEFAULT_CORPUS, DEFAULT_RUNS, load_corpus  # noqa: E402
from scripts.eval_summary_quality import (  # noqa: E402
    DEFAULT_MIN_BODY_WORDS,
    judge_one,
)

# A figure distinctive enough that its presence in the source can be checked,
# and implausible enough that no real article carries it by coincidence.
_FABRICATED = "The measure passed by a vote of 4,817 to 3."
_FABRICATED_MARK = "4,817"

_LEGAL_OVERCLAIMS = [
    (r"\bcharged with\b", "guilty of"),
    (r"\baccused of\b", "convicted of"),
    (r"\ballegedly\b", "definitely"),
    (r"\balleged\b", "confirmed"),
    (r"\bis under investigation for\b", "was found to have committed"),
    (r"\bfaces charges\b", "has been convicted"),
    (r"\bsettled\b", "admitted liability in"),
]

_ATTRIBUTIONS = [
    r"^According to [^,]+,\s*",
    r"^[A-Z][^,]{2,40} (?:said|says|reports|reported|argues|claims) that\s*",
    r",? according to [^.,]+",
    r"\b(?:prosecutors|officials|police|the complaint|the lawsuit) (?:say|says|said|allege|alleges|alleged)\s+(?:that\s+)?",
]


def plant_supported(summary: str, article: str) -> str | None:
    """Append a fabricated figure — only if it is genuinely absent upstream."""
    if _FABRICATED_MARK in article or _FABRICATED_MARK in summary:
        return None
    return summary.rstrip() + " " + _FABRICATED


def plant_legal(summary: str, _article: str) -> str | None:
    for pattern, replacement in _LEGAL_OVERCLAIMS:
        if re.search(pattern, summary, re.I):
            return re.sub(pattern, replacement, summary, count=1, flags=re.I)
    return None


def plant_attribution(summary: str, _article: str) -> str | None:
    for pattern in _ATTRIBUTIONS:
        if re.search(pattern, summary, re.I):
            stripped = re.sub(pattern, "", summary, count=1, flags=re.I).strip()
            if stripped and stripped != summary.strip():
                return stripped[0].upper() + stripped[1:]
    return None


PLANTS = {
    "supported": plant_supported,
    "legal_safe": plant_legal,
    "attributed": plant_attribution,
}


async def main(corpus_path: Path, runs_path: Path, min_body: int,
               limit: int, concurrency: int) -> int:
    corpus = load_corpus(corpus_path)
    by_url = {a.source_url: a for a in corpus}
    incumbent = json.loads(runs_path.read_text())["runs"][0]

    items = [
        (u, by_url[u].raw_content, incumbent[u]["summary"])
        for u in by_url
        if u in incumbent and len((by_url[u].raw_content or "").split()) >= min_body
    ][:limit]
    if not items:
        raise SystemExit(
            f"No articles with {min_body}+ body words and a recorded summary. "
            f"Re-run --sample and --self-agreement --save-runs first."
        )
    print(f"  {len(items)} articles with {min_body}+ words of body text\n")

    sem = asyncio.Semaphore(concurrency)

    # ── specificity: does it object to unmodified summaries? ──
    print("  SPECIFICITY — unmodified summaries, judged as-is")
    clean = await asyncio.gather(*(judge_one(sem, a, s) for _, a, s in items))
    ok = [v for v in clean if v]
    for axis in ("supported", "legal_safe", "attributed"):
        flagged = sum(1 for v in ok if not v[axis])
        print(f"    {axis:12s} flagged {flagged:3d}/{len(ok)}  "
              f"({flagged / max(len(ok), 1):.1%} false-positive rate)")

    # ── sensitivity: does it catch a violation it must catch? ──
    print("\n  SENSITIVITY — one planted violation per axis, true by construction")
    results: dict[str, tuple[int, int]] = {}
    for axis, plant in PLANTS.items():
        planted = []
        for _u, article, summary in items:
            bad = plant(summary, article)
            if bad:
                planted.append((article, bad))
        if not planted:
            print(f"    {axis:12s} no summaries carried a plantable pattern")
            continue
        verdicts = await asyncio.gather(
            *(judge_one(sem, a, s) for a, s in planted)
        )
        got = [v for v in verdicts if v]
        caught = sum(1 for v in got if not v[axis])
        results[axis] = (caught, len(got))
        print(f"    {axis:12s} caught {caught:3d}/{len(got)}  "
              f"({caught / max(len(got), 1):.1%} detection)")

    print("\n  VERDICT")
    weak = [a for a, (c, n) in results.items() if n and c / n < 0.80]
    if weak:
        print(f"    Detection below 80% on: {', '.join(weak)}.")
        print("    The judge cannot reliably find violations it is shown, so a")
        print("    pass rate on that axis says nothing. Fix the rubric before")
        print("    reporting any comparison that leans on it.")
    else:
        print("    Detection >= 80% on every plantable axis.")
    fp = {a: sum(1 for v in ok if not v[a]) / max(len(ok), 1)
          for a in ("supported", "legal_safe", "attributed")}
    noisy = [a for a, r in fp.items() if r > 0.25]
    if noisy:
        print(f"    False-positive rate above 25% on: {', '.join(noisy)}.")
        print("    The rubric objects to clean text often enough that a")
        print("    model-vs-model gap would be swamped by its own noise.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    p.add_argument("--min-body-words", type=int, default=DEFAULT_MIN_BODY_WORDS)
    p.add_argument("--limit", type=int, default=44)
    p.add_argument("--concurrency", type=int, default=6)
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.corpus, a.runs, a.min_body_words,
                              a.limit, a.concurrency)))
