#!/usr/bin/env python3
"""Is a candidate's summary FAITHFUL to the article it summarizes?

WHY FAITHFULNESS AND NOT "WHICH IS BETTER"
-------------------------------------------
A summary that asserts something the article does not say is the failure that
reaches readers. `services/summarizer._build_prompt` carries explicit rules
about exactly this — never characterize a legal outcome beyond what the source
says, attribute contested claims, a charge is not a conviction — because the
cards are about real people and a fabricated detail is a defamation risk, not a
style complaint.

It is also the measurement least vulnerable to the confound that makes
cross-vendor judging hard. Asking "which of these two summaries is better"
invites a judge to prefer its own family's voice. Asking "is this specific
claim supported by this specific text, yes or no" is close to a factual lookup,
and `services/judge.py` already uses that shape — three boolean axes rather
than a preference.

HOW BIAS IS HANDLED
-------------------
1. BLIND. The judge never learns which model wrote a summary, and the two
   models' outputs are interleaved and content-hash shuffled so position
   carries no signal either.
2. IDENTITY CONTROL. Before any comparison, the same summary is judged twice
   under different ids. The disagreement rate is the judge's own noise floor,
   and any gap between models smaller than it is not a finding. This is the
   same discipline docs/SOURCE_SCALING.md records for the linker: measure the
   instrument before trusting the reading.
3. The judge is Sonnet, from the same family as the incumbent. That is a real
   confound for a preference test and a weak one for a supported/unsupported
   lookup — but it is not zero, and the identity control is what bounds it.
   A second judge from another family is the honest next step if the gap is
   narrow.

WHAT THIS DOES NOT MEASURE
--------------------------
Readability, tone, usefulness, or whether the summary picked the RIGHT facts to
carry. A faithful summary can still be a bad one. This bounds harm, not merit.

Usage:
    ./.venv/bin/python3 scripts/eval_summary_quality.py \\
        --candidate data/eval/summarizer_deepseek.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.eval_summarizer import DEFAULT_CORPUS, DEFAULT_RUNS, load_corpus  # noqa: E402
from services import llm_client, model_registry  # noqa: E402

JUDGE = "sonnet-4-6"
CONCURRENCY = 6

# Below this much body text, "is every claim supported by the article" stops
# being a question about the model and becomes one about the feed.
DEFAULT_MIN_BODY_WORDS = 100
# Fewer eligible articles than this and the result is not worth reporting.
MIN_ELIGIBLE = 40

RUBRIC = """You are checking whether a one-or-two sentence news summary is \
FAITHFUL to the article text it was written from.

ARTICLE:
{article}

SUMMARY:
{summary}

Answer three questions about the SUMMARY, using ONLY the ARTICLE above as the \
source of truth. Do not use outside knowledge; if the article does not say it, \
it is not supported.

1. "supported" — Is every factual claim in the summary stated or directly \
implied by the article? false if it adds a number, name, cause, motive, \
outcome or consequence the article does not contain.

2. "legal_safe" — Does the summary avoid characterizing any legal matter \
beyond what the article literally says? false if a charge becomes guilt, an \
allegation becomes fact, an investigation becomes a finding, or a settlement \
becomes an admission. true if no legal matter is involved.

3. "attributed" — Where the summary states a contested claim, does it keep the \
article's attribution ("prosecutors say", "according to X")? false if the \
article attributes a claim and the summary asserts it flat. true if there is \
no contested claim.

Return ONLY this JSON object:
{{"supported": true/false, "legal_safe": true/false, "attributed": true/false, \
"why": "<= 15 words, only if any answer is false"}}"""


async def judge_one(sem, article_text: str, summary: str) -> dict | None:
    spec = model_registry.MODELS[JUDGE]
    async with sem:
        try:
            resp = await llm_client.complete(
                operation="judge.batch",
                user=RUBRIC.format(article=article_text[:4000], summary=summary),
                max_tokens=200,
                spec=spec,
            )
        except llm_client.LLMClientError as e:
            print(f"    judge error: {str(e)[:100]}", file=sys.stderr)
            return None
    text = resp.text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        d = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not all(k in d for k in ("supported", "legal_safe", "attributed")):
        return None
    return d


def _tally(verdicts: list[dict]) -> dict:
    n = len(verdicts) or 1
    clean = sum(
        v["supported"] and v["legal_safe"] and v["attributed"] for v in verdicts
    )
    return {
        "n": len(verdicts),
        "supported": sum(v["supported"] for v in verdicts) / n,
        "legal_safe": sum(v["legal_safe"] for v in verdicts) / n,
        "attributed": sum(v["attributed"] for v in verdicts) / n,
        "clean_all_three": clean / n,
    }


async def identity_control(sem, items: list[tuple[str, str]], n: int) -> float:
    """Judge the SAME summary twice. Disagreement here is the judge's own noise.

    Any gap between models smaller than this is not a finding — the same
    argument docs/SOURCE_SCALING.md makes for the linker's 97.3% self-agreement.
    """
    sample = items[:n]
    first = await asyncio.gather(*(judge_one(sem, a, s) for a, s in sample))
    second = await asyncio.gather(*(judge_one(sem, a, s) for a, s in sample))

    pairs = [(x, y) for x, y in zip(first, second, strict=True) if x and y]
    if not pairs:
        return 0.0
    agree = sum(
        x["supported"] == y["supported"]
        and x["legal_safe"] == y["legal_safe"]
        and x["attributed"] == y["attributed"]
        for x, y in pairs
    )
    return agree / len(pairs)


async def main(candidate_path: Path, corpus_path: Path, runs_path: Path,
               limit: int, control_n: int, min_body_words: int) -> int:
    corpus = load_corpus(corpus_path)
    by_url = {a.source_url: a for a in corpus}
    incumbent = json.loads(runs_path.read_text())["runs"][0]
    cand_blob = json.loads(candidate_path.read_text())
    candidate = cand_blob["results"]

    # GUARD, added after the first run produced a number that was not real.
    #
    # That run reported the INCUMBENT at 0.288 "supported" — 71% of Sift's live
    # summaries supposedly containing unsupported claims. The product would be
    # visibly broken if that were true. It was not measuring hallucination; it
    # was measuring the RSS feeds being stubs. 95% of the corpus carries under
    # 60 words of body text, the median is 25, and one article's entire body is
    # the literal string 'null'. Asking "is every claim in this summary stated
    # by the article" when the article is a 25-word teaser makes any compression
    # look unsupported.
    #
    # So the rubric needs real source text, and this refuses to run without it
    # rather than emitting a confident number again.
    eligible = [
        u for u in by_url
        if u in incumbent and u in candidate
        and len((by_url[u].raw_content or "").split()) >= min_body_words
    ]
    total = sum(1 for u in by_url if u in incumbent and u in candidate)
    print(f"  corpus {len(by_url)}   with body >= {min_body_words} words: "
          f"{len(eligible)}/{total}")
    if len(eligible) < MIN_ELIGIBLE:
        raise SystemExit(
            f"\n  REFUSING TO RUN: only {len(eligible)} articles carry "
            f"{min_body_words}+ words of body text, and {MIN_ELIGIBLE} is the "
            f"floor for a number worth reporting.\n\n"
            f"  This is a property of Sift's inputs, not of this script. RSS\n"
            f"  feeds mostly ship teasers: the corpus median is ~25 words, so\n"
            f"  there is not enough source text to verify a summary against.\n\n"
            f"  Options:\n"
            f"    - capture a corpus filtered for long bodies (changes the\n"
            f"      distribution away from production, and must be said so)\n"
            f"    - fetch article bodies rather than relying on the RSS field\n"
            f"    - judge a different axis: contradiction rather than support,\n"
            f"      which a teaser CAN adjudicate\n"
            f"    - lower --min-body-words and accept the result measures\n"
            f"      compression from sparse input, not faithfulness"
        )
    urls = eligible[:limit]
    print(f"  judged {len(urls)} articles x 2 models")
    print(f"  candidate: {cand_blob['catalog_id']} ({cand_blob['model']})")
    print(f"  judge:     {model_registry.MODELS[JUDGE].model}")

    sem = asyncio.Semaphore(CONCURRENCY)

    print(f"\n  identity control ({control_n} summaries judged twice)...")
    control_items = [(by_url[u].raw_content, incumbent[u]["summary"]) for u in urls]
    self_agree = await identity_control(sem, control_items, control_n)
    print(f"    judge self-agreement   {self_agree:.3f}  "
          f"(noise floor: {1 - self_agree:.1%})")

    # Blind and shuffled: the judge sees one summary at a time with no label,
    # and the order is content-hashed so it carries no signal.
    work = []
    for u in urls:
        for who, summ in (("incumbent", incumbent[u]["summary"]),
                          ("candidate", candidate[u]["summary"])):
            work.append((who, u, by_url[u].raw_content, summ))
    work.sort(key=lambda w: hashlib.sha256(f"{w[1]}{w[0]}".encode()).hexdigest())

    print(f"\n  judging {len(work)} summaries blind...")
    results = await asyncio.gather(*(judge_one(sem, a, s) for _, _, a, s in work))

    buckets: dict[str, list[dict]] = {"incumbent": [], "candidate": []}
    failures = []
    for (who, u, _, summ), v in zip(work, results, strict=True):
        if v is None:
            continue
        buckets[who].append(v)
        if not (v["supported"] and v["legal_safe"] and v["attributed"]):
            failures.append((who, u, summ, v))

    inc, cand = _tally(buckets["incumbent"]), _tally(buckets["candidate"])
    print(f"\n  {'axis':22s} {'incumbent':>10s} {'candidate':>10s} {'delta':>8s}")
    for k in ("supported", "legal_safe", "attributed", "clean_all_three"):
        d = cand[k] - inc[k]
        print(f"  {k:22s} {inc[k]:10.3f} {cand[k]:10.3f} {d:+8.3f}")
    print(f"  {'n judged':22s} {inc['n']:10d} {cand['n']:10d}")

    gap = abs(cand["clean_all_three"] - inc["clean_all_three"])
    noise = 1 - self_agree
    print(f"\n  gap on clean_all_three   {gap:.3f}")
    print(f"  judge noise floor        {noise:.3f}")
    if gap <= noise:
        print("  => INDISTINGUISHABLE. The gap is inside the judge's own\n"
              "     disagreement-with-itself, so it is not evidence either way.")
    else:
        better = "candidate" if cand["clean_all_three"] > inc["clean_all_three"] else "incumbent"
        print(f"  => the {better} is cleaner by more than the judge's noise floor.")

    if failures:
        print(f"\n  {len(failures)} summaries failed at least one axis. First 6:")
        for who, _url, summ, v in failures[:6]:
            axes = [k for k in ("supported", "legal_safe", "attributed") if not v[k]]
            print(f"\n    [{who}] {','.join(axes)} — {v.get('why', '')[:70]}")
            print(f"      {summ[:150]}")

    print(
        "\n  WHAT THIS DOES NOT SAY. Faithfulness is not merit: a summary can be\n"
        "  perfectly supported and still pick the wrong facts, read badly, or\n"
        "  bury the point. This bounds harm, not quality. And the judge is from\n"
        "  the incumbent's own family — weak confound for a supported/not\n"
        "  lookup, not zero, which is what the identity control bounds."
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    p.add_argument("--limit", type=int, default=150)
    p.add_argument("--control", type=int, default=25)
    p.add_argument("--min-body-words", type=int, default=DEFAULT_MIN_BODY_WORDS)
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.candidate, a.corpus, a.runs, a.limit, a.control,
                              a.min_body_words)))
