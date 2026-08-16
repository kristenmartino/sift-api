#!/usr/bin/env python3
"""Emit blind summary pairs for a human to read, plus a key to score them with.

WHY BLIND
---------
Every automated result so far bounds harm rather than merit. `supported` says
neither model fabricates more than the other; it says nothing about whether a
summary picks the right facts, reads well, or sounds like Sift. Only a person
can answer that, and only if they do not know which model wrote which — an
unblinded read anchors on whichever label the reader already trusts.

Same discipline as `eval_clustering.review_sample` and
`scripts/eval_ranking_pairs.py`: the pair order is content-hashed rather than
random, so the sheet is reproducible, and the key lands in a separate file that
the reader does not open until the verdicts are filled in.

Usage:
    ./.venv/bin/python3 scripts/review_summaries.py --n 50
    # read data/eval/summary_review.md, fill verdicts into the CSV, then:
    ./.venv/bin/python3 scripts/review_summaries.py --score
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.eval_summarizer import DEFAULT_CORPUS, DEFAULT_RUNS, load_corpus  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SHEET_MD = REPO / "data" / "eval" / "summary_review.md"
SHEET_CSV = REPO / "data" / "eval" / "summary_review.csv"
KEY = REPO / "data" / "eval" / "summary_review.key.json"


def build(candidate_path: Path, corpus_path: Path, runs_path: Path, n: int) -> None:
    corpus = load_corpus(corpus_path)
    by_url = {a.source_url: a for a in corpus}
    incumbent = json.loads(runs_path.read_text())["runs"][0]
    blob = json.loads(candidate_path.read_text())
    candidate = blob["results"]

    usable = [u for u in by_url if u in incumbent and u in candidate]

    # Round-robin across outlets: the top 10 sources are 64% of volume, so a
    # straight draw would be mostly Sports Illustrated and the New York Post.
    by_source: dict[str, list[str]] = defaultdict(list)
    for u in usable:
        by_source[by_url[u].source_name].append(u)
    picked, rnd = [], 0
    while len(picked) < n:
        added = False
        for src in sorted(by_source):
            if rnd < len(by_source[src]) and len(picked) < n:
                picked.append(by_source[src][rnd])
                added = True
        if not added:
            break
        rnd += 1

    key = {}
    rows = []
    md = [
        "# Blind summary review",
        "",
        f"{len(picked)} articles. For each, **A** and **B** are the same article "
        "summarized by two different models — which one varies per item, so do "
        "not look for a pattern.",
        "",
        "Fill `verdict` in `summary_review.csv` with **A**, **B**, or **same**. "
        "Judge whatever you actually care about: does it carry the right fact, "
        "does it read like Sift, would you ship it.",
        "",
        "Do not open `summary_review.key.json` until you are done.",
        "",
        "---",
        "",
    ]

    for i, u in enumerate(picked, 1):
        art = by_url[u]
        inc, cand = incumbent[u]["summary"], candidate[u]["summary"]
        # Content-hashed, so the sheet is reproducible and the order carries no
        # signal about which model produced which side.
        flip = int(hashlib.sha256(u.encode()).hexdigest(), 16) % 2 == 1
        a, b = (cand, inc) if flip else (inc, cand)
        key[str(i)] = {"A": "candidate" if flip else "incumbent",
                       "B": "incumbent" if flip else "candidate",
                       "url": u}
        rows.append({"n": i, "source": art.source_name,
                     "title": art.title, "verdict": ""})
        md += [
            f"### {i}. {art.title}",
            f"*{art.source_name}*",
            "",
            f"**A.** {a}",
            "",
            f"**B.** {b}",
            "",
            "---",
            "",
        ]

    SHEET_MD.parent.mkdir(parents=True, exist_ok=True)
    SHEET_MD.write_text("\n".join(md))
    with SHEET_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["n", "source", "title", "verdict"])
        w.writeheader()
        w.writerows(rows)
    KEY.write_text(json.dumps(
        {"candidate": blob["catalog_id"], "model": blob["model"], "key": key},
        indent=2) + "\n")

    print(f"  wrote {len(picked)} pairs across "
          f"{len({by_url[u].source_name for u in picked})} outlets")
    print(f"    read   {SHEET_MD}")
    print(f"    score  {SHEET_CSV}   (fill the verdict column: A / B / same)")
    print(f"    key    {KEY}   — do not open until done")


def score() -> None:
    if not SHEET_CSV.exists() or not KEY.exists():
        raise SystemExit("No sheet to score. Run without --score first.")
    k = json.loads(KEY.read_text())
    rows = list(csv.DictReader(SHEET_CSV.open(encoding="utf-8-sig")))
    filled = [r for r in rows if (r.get("verdict") or "").strip()]
    if not filled:
        raise SystemExit(
            f"0 of {len(rows)} verdicts filled in {SHEET_CSV}. Nothing to score."
        )

    tally: Counter = Counter()
    for r in filled:
        v = r["verdict"].strip().upper()
        if v == "SAME":
            tally["same"] += 1
        elif v in ("A", "B"):
            tally[k["key"][r["n"]][v]] += 1

    n = sum(tally.values())
    print(f"\n  {len(filled)}/{len(rows)} verdicts filled  "
          f"(candidate = {k['candidate']})\n")
    for label in ("incumbent", "candidate", "same"):
        c = tally[label]
        print(f"    {label:12s} {c:3d}  ({c / max(n, 1):.0%})")

    decided = tally["incumbent"] + tally["candidate"]
    if decided < 20:
        print(f"\n  Only {decided} decided (non-'same') verdicts. Read more "
              f"before concluding — a preference this thin is a coin flip.")
        return
    lead = abs(tally["candidate"] - tally["incumbent"])
    # Two-sided sign test approximation on the decided pairs.
    import math
    se = math.sqrt(decided) / 2
    print(f"\n  decided {decided}, margin {lead}, ~{lead / max(se, 1e-9):.1f} SE")
    if lead < 1.96 * se:
        print("  => no clear preference. The split is within what coin flips give.")
    else:
        winner = "candidate" if tally["candidate"] > tally["incumbent"] else "incumbent"
        print(f"  => you preferred the {winner}, beyond chance.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path,
                   default=REPO / "data" / "eval" / "summarizer_deepseek.json")
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--score", action="store_true")
    a = p.parse_args()
    if a.score:
        score()
    else:
        build(a.candidate, a.corpus, a.runs, a.n)
