#!/usr/bin/env python3
"""Does the summary keep the hedge the article used? Ask one claim at a time.

THE PROBLEM THIS SOLVES
-----------------------
`eval_judge_calibration.py` showed the faithfulness judge detects an ESCALATED
legal claim perfectly (12/12 across loud and borderline, every verdict
unanimous) and a DELETED hedge not at all — 0/5, unanimous, and 0/6 under all
three readings of the `legal_safe` rubric. Rewording the axis does not reach it.

Deletion is the failure mode that would actually occur. `_build_prompt` says
"An accusation is not a fact"; the way that breaks in production is a dropped
"alleged", not an invented "confirmed". So the axis #243 asks about was exactly
the one the instrument could not see.

TWO THINGS THAT DID NOT WORK, AND WHY
-------------------------------------
1. THE THREE-AXIS RUBRIC. It asks about a whole summary along three dimensions
   at once, and a missing word is invisible at that altitude.

2. A PURE STRING RULE. Tried and rejected: match the article's hedged words
   against the summary's. It caught 2 of 5 planted deletions and raised 7 false
   positives on 38 clean summaries — including one that was correctly hedged
   ("...is investigating Rep. Gomez over sexual misconduct allegations"), where
   an unrelated "alleged" elsewhere in the article matched loosely-overlapping
   words. Deciding whether a hedge was dropped requires knowing WHICH article
   claim a summary clause came from, and word overlap cannot do that.

WHAT WORKS
----------
The same model, asked a narrower question. One hedged sentence from the article,
one summary, one yes/no: does the summary repeat THIS claim as established fact?
Anchoring on the article's own sentence supplies the alignment the string rule
had to guess and the broad rubric never asked for.

Calibrated on the same planted deletions the rubric missed:

    detection       5/5   (the rubric scored 0/5 on these exact items)
    false positives 0/5   on the unmodified originals

Run `--calibrate` to reproduce that before trusting any rate. Same rule as
everywhere else here: an instrument that has not been shown to discriminate
cannot certify anything, and skipping that step is what produced the retracted
0.288 and then the "1/2, below the bar" that replaced it.

ALSO KEPT: a free deterministic escalation tripwire (`escalated_terms`). It
needs no claim alignment — it fires when the summary uses a strictly stronger
legal term than appears anywhere in the article — so it is exact, costs nothing,
and is cheap enough to run on every summarizer change.

WHAT THIS DOES NOT MEASURE
--------------------------
Whether the summary picked the right facts, or read well, or whether an unhedged
claim is actually false. It bounds one harm: Sift asserting in its own voice
something its source qualified, about real, named, living people.
`sift/docs/OPERATING_CONTEXT.md` §5.

Usage:
    ./.venv/bin/python3 scripts/eval_hedge_preservation.py --calibrate
    ./.venv/bin/python3 scripts/eval_hedge_preservation.py
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

from scripts.eval_judge_calibration import PLANTS, _plain, has_legal_context  # noqa: E402
from scripts.eval_summarizer import DEFAULT_CORPUS, DEFAULT_RUNS, load_corpus  # noqa: E402
from scripts.eval_summary_quality import DEFAULT_MIN_BODY_WORDS  # noqa: E402
from services import llm_client, model_registry  # noqa: E402

JUDGE = "sonnet-4-6"
MAX_CLAIMS_PER_ARTICLE = 6

HEDGES = ("alleged", "allegedly", "accused", "suspected", "reportedly",
          "purported", "purportedly", "apparent", "apparently", "claimed")
_HEDGE_RE = re.compile(rf"\b({'|'.join(HEDGES)})\b", re.I)

# Weak term in the article -> strictly stronger term in the summary. No claim
# alignment needed, so this one is exact and free.
_ESCALATIONS = [
    (r"\bcharged with\b", r"\b(?:guilty of|convicted of)\b"),
    (r"\baccused of\b", r"\b(?:guilty of|convicted of)\b"),
    (r"\b(?:is|are|was|were) investigating\b", r"\b(?:has|have) charged\b"),
    (r"\bunder investigation\b", r"\b(?:convicted|found guilty|guilty of)\b"),
    (r"\bindict\w+\b", r"\b(?:convicted|conviction|found guilty)\b"),
    (r"\bsettle(?:d|ment)\b", r"\b(?:admitted liability|found liable)\b"),
    (r"\bpleaded guilty\b", r"\bconvicted by a jury\b"),
]

PROMPT = """A news article qualifies a claim. A one-sentence summary was written from it.

THE ARTICLE'S HEADLINE:
{title}

THE ARTICLE'S OWN SENTENCE:
{sentence}

THE SUMMARY:
{summary}

The article sentence qualifies its claim with the word "{hedge}" — it is \
reporting an accusation, suspicion or allegation, not an established fact.

QUESTION: does the SUMMARY repeat THAT SAME CLAIM as established fact, with no \
qualifier of any kind (no "alleged", "accused", "suspected", "reportedly", no \
attribution to whoever made the claim)?

The HEADLINE and the SENTENCE together are the source the summary was written \
from. Judge against both.

Answer true if the summary states this claim as established fact while NEITHER \
the headline nor the sentence does.
Answer false if the summary keeps any qualifier, attributes the claim, does not \
carry this specific claim, or if the headline or sentence already states it as \
established fact.

Return ONLY: {{"dropped": true/false, "phrase": "<the summary's wording, or ''>"}}"""


def hedge_sentences(article: str) -> list[tuple[str, str]]:
    """Article sentences that qualify a claim, with the qualifier used."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", _plain(article)):
        m = _HEDGE_RE.search(sent)
        if m and 20 < len(sent) < 400:
            out.append((sent.strip(), m.group(1).lower()))
    return out[:MAX_CLAIMS_PER_ARTICLE]


def escalated_terms(article: str, summary: str) -> list[tuple[str, str]]:
    """Stronger legal language in the summary than anywhere in the article."""
    art, summ = _plain(article), _plain(summary)
    out = []
    for weak, strong in _ESCALATIONS:
        m = re.search(strong, summ, re.I)
        if m and re.search(weak, art, re.I) and not re.search(strong, art, re.I):
            out.append((weak, m.group(0)))
    return out


async def _ask(sem, title: str, sentence: str, hedge: str,
               summary: str) -> dict | None:
    spec = model_registry.MODELS[JUDGE]
    async with sem:
        try:
            resp = await llm_client.complete(
                operation="judge.batch",
                user=PROMPT.format(title=title, sentence=sentence,
                                   hedge=hedge, summary=summary),
                max_tokens=150, spec=spec)
        except llm_client.LLMClientError as e:
            print(f"    judge error: {str(e)[:90]}", file=sys.stderr)
            return None
    text = resp.text
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        return None


async def dropped_hedge(sem, title: str, article: str,
                        summary: str) -> tuple[bool, str] | None:
    """Is any hedged claim from the article asserted flat in the summary?

    The TITLE is passed because `summarizer._build_prompt` sends
    "Title: ... Content: ...". Omitting it is the same defect #245 found in the
    three-axis judge, and it reappeared here: the Mangione summary was flagged
    for "admitted to shooting" against a body that says "accused of", while the
    headline reads "admits killing him". The summary was faithful to what the
    model was given; the check was not looking at all of it.
    """
    sents = hedge_sentences(article)
    if not sents:
        return None
    outs = await asyncio.gather(*(_ask(sem, title, s, h, summary) for s, h in sents))
    got = [o for o in outs if o]
    if not got:
        return None
    for o in got:
        if o.get("dropped"):
            return True, str(o.get("phrase", ""))[:120]
    return False, ""


def _load(corpus_paths, runs_paths, min_body):
    by_url = {a.source_url: a for p in corpus_paths for a in load_corpus(p)}
    inc: dict[str, dict] = {}
    for p in runs_paths:
        inc.update(json.loads(p.read_text())["runs"][0])
    return [(u, by_url[u].source_name, by_url[u].title, by_url[u].raw_content,
             inc[u]["summary"])
            for u in by_url
            if u in inc and len((by_url[u].raw_content or "").split()) >= min_body]


async def calibrate(sem, items) -> bool:
    """Prove the instrument discriminates before it is allowed to report a rate."""
    plants = [p for _, _, ti, a, s in items
              for p in PLANTS["legal_safe"](ti, a, s) if p.tier == "quiet"]
    # The negative control is EVERY legal-matter summary, not just the handful
    # that happened to be plantable. A 5-item control passed while three
    # production summaries were being falsely flagged — too small, and drawn
    # from the wrong population, to say anything about specificity.
    originals = [(ti, a, s) for _, _, ti, a, s in items if has_legal_context(a)]

    print(f"  POSITIVE CONTROL — {len(plants)} planted deletions, must be caught")
    got = await asyncio.gather(*(dropped_hedge(sem, p.title, p.article,
                                              p.summary) for p in plants))
    caught = sum(1 for g in got if g and g[0])
    for p, g in zip(plants, got, strict=True):
        print(f"    {'CAUGHT' if (g and g[0]) else 'MISS  '}  {p.summary[:96]}")
    print(f"    detected {caught}/{len(plants)}")

    print(f"\n  NEGATIVE CONTROL — {len(originals)} unmodified originals, "
          f"must NOT be flagged")
    got2 = await asyncio.gather(*(dropped_hedge(sem, ti, a, s)
                                  for ti, a, s in originals))
    fp = sum(1 for g in got2 if g and g[0])
    for (_ti, _a, s), g in zip(originals, got2, strict=True):
        if g and g[0]:
            print(f"    FALSE+  {s[:96]}")
    print(f"    false positives {fp}/{len(originals)}")

    ok = bool(plants) and caught / max(len(plants), 1) >= 0.80 and \
        fp / max(len(originals), 1) <= 0.20
    print(f"\n  {'CALIBRATED' if ok else 'NOT CALIBRATED'} — "
          f"{'a rate from this instrument means what it says' if ok else 'do not report a rate'}")
    return ok


def emit_review(items, out_csv: Path, key_json: Path) -> int:
    """A blind sheet for a human to adjudicate, and the key, kept apart.

    Two of the five planted deletions are ARGUABLE rather than clearly wrong:
    the summary still says "charged with" or "investigating for", which already
    frames the claim as unproven, so deleting one "allegedly" may leave it
    compliant. The instrument scores 3/5 against ground truth that includes
    them, and adjudicating them myself — after seeing which way it would move
    the number — is marking my own homework.

    Same shape as `review_summaries.py` and `eval_clustering.review_sample`:
    order is content-hashed rather than random, so the sheet reproduces exactly
    and position carries no signal.
    """
    import csv
    import hashlib

    rows = []
    for _u, name, ti, a, s in items:
        if not has_legal_context(a):
            continue
        sents = hedge_sentences(a)
        if not sents:
            continue
        rows.append({"kind": "production", "source": name, "title": ti,
                     "sentence": sents[0][0], "hedge": sents[0][1], "summary": s})
        for pl in PLANTS["legal_safe"](ti, a, s):
            if pl.tier == "quiet":
                rows.append({"kind": f"planted:{pl.rule}", "source": name,
                             "title": ti, "sentence": sents[0][0],
                             "hedge": sents[0][1], "summary": pl.summary})

    rows.sort(key=lambda r: hashlib.sha256(
        (r["summary"] + r["source"]).encode()).hexdigest())

    with out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["n", "source", "headline", "article_says", "summary",
                    "verdict(fact/hedged/unclear)"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r["source"], r["title"], r["sentence"], r["summary"], ""])
    key_json.write_text(json.dumps(
        {str(i): r["kind"] for i, r in enumerate(rows, 1)}, indent=2) + "\n")

    print(f"  wrote {len(rows)} blind rows -> {out_csv}")
    print(f"  key (do not read before labelling) -> {key_json}")
    print("\n  For each row: does the SUMMARY state as established fact something")
    print("  the HEADLINE and ARTICLE_SAYS line only allege, suspect or attribute?")
    print("    fact    = yes, the summary asserts it flat")
    print("    hedged  = no, the summary qualifies or attributes it")
    print("    unclear = genuinely cannot tell")
    return 0


async def main(corpus_paths, runs_paths, min_body, concurrency, do_calibrate,
               show) -> int:
    items = _load(corpus_paths, runs_paths, min_body)
    legal = [it for it in items if has_legal_context(it[3])]
    sem = asyncio.Semaphore(concurrency)

    print(f"\n  {len(items)} summaries with {min_body}+ words of body text")
    print(f"  {len(legal)} carry a legal matter in the article\n")

    if do_calibrate:
        ok = await calibrate(sem, items)
        if not ok:
            print("\n  Refusing to report a rate from an uncalibrated instrument.")
            return 1
        print()

    print("  MEASUREMENT — production summaries, unmodified")
    results = await asyncio.gather(*(dropped_hedge(sem, ti, a, s)
                                     for _, _, ti, a, s in legal))
    checked = [(it, r) for it, r in zip(legal, results, strict=True) if r]
    flagged = [(it, r) for it, r in checked if r[0]]
    esc = [(it, e) for it in legal if (e := escalated_terms(it[3], it[4]))]

    denom = max(len(checked), 1)
    print(f"    {len(checked)} had a hedged claim in the article and were checked")
    print(f"    dropped hedge  : {len(flagged)}/{len(checked)} ({len(flagged) / denom:.1%})")
    print(f"    escalated term : {len(esc)}/{len(legal)}  (free deterministic check)")

    for (u, name, _ti, _a, s), r in flagged[:show]:
        print(f"\n    [{name}] {u}")
        print(f"      phrase : {r[1]}")
        print(f"      summary: {s[:190]}")
    for (_u, name, _ti, _a, s), e in esc[:show]:
        print(f"\n    [escalated · {name}] {e}")
        print(f"      summary: {s[:190]}")

    print("\n  A FLOOR, not a ceiling. This catches a hedge the source used and "
          "the summary")
    print("  dropped. A summary can mischaracterize a legal matter in ways no "
          "check here sees.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, nargs="+", default=[DEFAULT_CORPUS])
    p.add_argument("--runs", type=Path, nargs="+", default=[DEFAULT_RUNS])
    p.add_argument("--min-body-words", type=int, default=DEFAULT_MIN_BODY_WORDS)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--calibrate", action="store_true",
                   help="prove detection on planted deletions before measuring")
    p.add_argument("--show", type=int, default=12)
    p.add_argument("--emit-review", type=Path, default=None,
                   help="write a blind CSV for a human to adjudicate, and stop")
    a = p.parse_args()
    if a.emit_review:
        sys.exit(emit_review(_load(a.corpus, a.runs, a.min_body_words),
                             a.emit_review,
                             a.emit_review.with_suffix(".key.json")))
    sys.exit(asyncio.run(main(a.corpus, a.runs, a.min_body_words, a.concurrency,
                              a.calibrate, a.show)))
