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

So this measures the other one:

  SENSITIVITY — plant a violation the judge MUST catch, and see if it does.
  SPECIFICITY — run the judge on text that is NOT a violation and see how
    often it objects anyway.

Both are needed. Sensitivity alone would pass a judge that flags everything;
specificity alone would pass one that flags nothing.

WHAT CHANGED, AND WHY (#243)
----------------------------
The first version of this file calibrated `supported` successfully and reported
`legal_safe 1/2` and `attributed 0/1` — both below its own 80% bar — concluding
that the corpus lacked plantable legal language and a richer corpus was needed.

Both halves of that were wrong, and the second one matters more.

1. THE PREDICATE WAS TOO NARROW, NOT THE CORPUS TOO CLEAN. Every pattern in
   `_LEGAL_OVERCLAIMS` required an active-voice hedged verb: "is under
   investigation for" matched, "is investigating" did not; "charged with"
   matched, "pleaded guilty to ... charges" did not. Nothing matched passive
   ("being investigated for"), nominal ("federal indictment charges"), or
   "effort to prosecute". Widening to those forms takes plantable summaries
   from 2 to 7 on the very same committed corpus.

2. THE GROUND TRUTH WAS UNSOUND. A plant is only a valid test if the edit
   violates THE AXIS BEING MEASURED. Plantability was decided from the summary
   alone — both planters took the article and ignored it, and the parameter was
   named `_article` to say so. So "RTVE allegedly skipped introducing an
   athlete" -> "definitely skipped" was planted as a `legal_safe` violation.
   There is no legal matter there: no charge, no court, no investigation. The
   rubric's own "true if no legal matter is involved" makes `true` correct, and
   a valid answer was recorded as a missed detection. That one bad test case is
   the whole difference between "1/1" and "1/2, below the bar".

   Plantability now consults the ARTICLE (`has_legal_context`, `_attributes`).

That defect is the retraction's defect one level down. The retracted number came
from a judge nobody had validated; the replacement came from a VALIDATOR nobody
had validated. Same question `sift/__tests__/meta.test.ts` and
`sift/stryker.conf.json` ask of the frontend suite: does this check run, or can
it actually fail? `tests/test_eval_judge_calibration.py` now pins it.

HOW THE VIOLATIONS ARE PLANTED
------------------------------
Deterministically, not by asking a model to corrupt text — that would make the
ground truth as uncertain as the thing being measured. Each edit is a mechanical
rewrite whose violation is true by construction, and it is applied only where
the ARTICLE establishes that the axis is engaged.

Graded by severity, because a single number hides where the judge's boundary is:

  loud        escalate to the extreme   "charged with" -> "guilty of"
  borderline  escalate by one step      "pleaded guilty to" -> "was convicted
                                        by a jury of" (a plea is not a trial)
  quiet       delete the hedge          "alleged gang members" -> "gang members"

Quiet is the realistic failure mode. A summarizer is far likelier to drop a
hedge than to invent the word "confirmed", so detection on `loud` alone is a
FLOOR — it proves the judge is not blind, not that it catches what production
actually does wrong.

NEGATIVE CONTROLS
-----------------
Detection alone would pass a judge that flags everything, so each engaged item
is also judged in a strictly-MORE-hedged form ("According to <outlet>, ...").
Hedging harder cannot over-characterize a legal matter, so `legal_safe` and
`attributed` must both stay true. Flags there are false positives on exactly
the population that matters.

Usage:
    ./.venv/bin/python3 scripts/eval_judge_calibration.py
    ./.venv/bin/python3 scripts/eval_judge_calibration.py --rubric legal-scoped
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.eval_summarizer import DEFAULT_CORPUS, DEFAULT_RUNS, load_corpus  # noqa: E402
from scripts.eval_summary_quality import (  # noqa: E402
    DEFAULT_MIN_BODY_WORDS,
    LEGAL_AXES,
    build_rubric,
    judge_one,
)

AXES = ("supported", "legal_safe", "attributed")
TIERS = ("loud", "borderline", "quiet")


def _plain(text: str) -> str:
    """Corpus `raw_content` carries raw HTML; the summarizer strips it too."""
    return html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


# ── is the axis engaged at all? ──────────────────────────────────────────
#
# `legal_safe` asks about characterizing a LEGAL MATTER. A hedge word is not a
# legal matter — "allegedly skipped an athlete introduction" is a faithfulness
# question, not a legal one. So the gate looks for a legal proceeding, record,
# or actor IN THE ARTICLE, and deliberately excludes bare "alleged"/"accused".

_LEGAL_CONTEXT = re.compile(r"""(?:\b(?:
 indict\w+ | convict(?:ed|ion|ions) | acquit\w+ | arraign\w+ | subpoena\w* |
 prosecut(?:e|es|ed|ing|or|ors|ion) | plaintiff\w* | defendant\w* |
 lawsuits? | litigation | sued | felony | felonies | misdemeanor\w* |
 grand\sjury | mistrial | courtroom | injunction | consent\sdecree |
 criminal\scharges? | ethics\scomplaint | restraining\sorder
 )\b)
 | (?:\b(?:pleaded|pled)\s(?:guilty|not\sguilty)\b)
 | (?:\bfaces\s(?:criminal\s)?charges\b) | (?:\bcharged\swith\b)
 | (?:\bfiled\s(?:a\s)?(?:suit|lawsuit|charges|complaint)\b)
 | (?:\b(?:found|held)\sliable\b) | (?:\blegal\sliability\b)
 | (?:\b(?:jury|judge)\s(?:found|ruled|convicted|acquitted|awarded)\b)
 | (?:\b(?:guilty|jury)\sverdict\b) | (?:\bverdict\s(?:in|of|was)\b)
 | (?:\b(?:appeals?|federal|district|supreme|circuit)\scourt\b)
 | (?:\bcourt\s(?:ruling|filing|filings|order|documents?|records?|case|hearing|papers)\b)
 | (?:\bin\scourt\b) | (?:\bbefore\sthe\scourt\b)
 | (?:\bthe\scourt\s(?:ruled|found|held|said|denied|granted|dismissed|heard|vacated)\b)
 | (?:\bsentenc(?:e|ed|ing)\s(?:to|for|is|was|scheduled|hearing)\b)
 | (?:\bsettle(?:d|ment)\s(?:with|of|over|agreement)\b)
 | (?:\battorney\sgeneral\b) | (?:\bdistrict\sattorney\b)
""", re.I | re.X)

# "investigation" alone is ambiguous — a BBC investigation is journalism, not
# law. It counts only when an authority is the one investigating.
_AUTHORITY = (r"(?:federal|state|criminal|congressional|senate|house|police|FBI|DOJ|"
              r"Justice\sDepartment|FTC|SEC|IRS|EPA|DHS|ICE|prosecutor\w*|"
              r"attorney\sgeneral|grand\sjury|inspector\sgeneral|ethics|watchdog|"
              r"regulator\w*|antitrust|internal\saffairs)")
_LEGAL_INVESTIGATION = re.compile(
    rf"\b(?:{_AUTHORITY}[\s\w,'’-]{{0,40}}?(?:investigat\w+|probe|inquiry)"
    rf"|(?:investigat\w+|probe|inquiry)[\s\w,'’-]{{0,40}}?by\s(?:the\s)?{_AUTHORITY})",
    re.I)


def has_legal_context(article: str) -> bool:
    """Does the ARTICLE describe an actual legal matter?

    Deliberately does NOT count bare "alleged"/"accused": those are hedges,
    and treating them as legal context is what planted the RTVE broadcaster
    item as a `legal_safe` violation.
    """
    text = _plain(article)
    return bool(_LEGAL_CONTEXT.search(text) or _LEGAL_INVESTIGATION.search(text))


# Who is doing the attributing, and the verbs that count as attribution.
_ATTRIB_SOURCE = (r"prosecutors|officials|police|investigators|authorities|"
                  r"the\scomplaint|the\slawsuit|the\sindictment|the\sfiling|"
                  r"the\sreport|the\scompany|researchers|critics|lawyers|"
                  r"the\sagency|the\sdepartment")
_ATTRIB_VERB = (r"say|says|said|allege|alleges|alleged|claim|claims|claimed|"
                r"report|reports|reported|argue|argues|argued|told|denies|denied")

# Applied to the SUMMARY, to find an attribution clause worth removing.
_ATTRIBUTIONS = [
    r"^According to ([^,]+),\s*",
    r",?\s*according to ([^.,;]+)",
    rf"\b({_ATTRIB_SOURCE})\s+(?:{_ATTRIB_VERB})\s+(?:that\s+)?",
    rf"^([A-Z][\w.'’\-]*(?:\s[\w.'’\-]+){{0,4}})\s+(?:{_ATTRIB_VERB})\s+(?:that\s+)?",
]


def _attributes(article: str, attributor: str) -> bool:
    """Does the ARTICLE itself attribute, to this same source?

    The rubric makes `attributed` false only "if the article attributes a claim
    and the summary asserts it flat". Stripping an attribution the article never
    made is not a violation of that, so the source noun must reappear in the
    article inside an attribution construction.
    """
    text = _plain(article)
    head = attributor.strip().strip(".,;:").split()
    if not head:
        return False
    # Match on the most specific word available (usually a proper noun).
    key = max(head, key=len)
    if len(key) < 3:
        return False
    return bool(re.search(rf"\b{re.escape(key)}\b[\s\w,'’\-]{{0,30}}?\b(?:{_ATTRIB_VERB})\b",
                          text, re.I)
                or re.search(rf"\baccording to\b[\s\w,'’\-]{{0,30}}?\b{re.escape(key)}\b",
                             text, re.I))


# ── the plants ───────────────────────────────────────────────────────────

# A figure distinctive enough that its presence in the source can be checked,
# and implausible enough that no real article carries it by coincidence.
_FABRICATED = "The measure passed by a vote of 4,817 to 3."
_FABRICATED_MARK = "4,817"

# Escalate a legal characterization to the extreme. Each is a violation
# regardless of what the article says, because none of these is ever a
# faithful rendering of the phrase it replaces.
_LEGAL_LOUD = [
    (r"\bcharged with\b", "guilty of"),
    (r"\baccused of\b", "convicted of"),
    (r"\ballegedly\b", "definitely"),
    (r"\balleged\b", "confirmed"),
    (r"\bis under investigation for\b", "was found to have committed"),
    (r"\bfaces charges\b", "has been convicted"),
    (r"\bsettled\b", "admitted liability in"),
    # widened: passive, nominal, and other-voice forms the original missed
    (r"\bis investigating\b", "has proven wrongdoing by"),
    (r"\bare investigating\b", "have proven wrongdoing by"),
    (r"\bbeing investigated for\b", "proven to have committed"),
    (r"\bunder investigation\b", "proven guilty"),
    (r"\bunder scrutiny\b", "proven guilty"),
    (r"\binvestigation into\b", "proof of guilt of"),
    (r"\bindictment\b", "conviction"),
    (r"\bindicted\b", "convicted"),
    (r"\bpotential (\w+) violations\b", r"confirmed \1 violations"),
    (r"\bfaces trial\b", "has been convicted"),
    (r"\bsued\b", "was found liable"),
]

# Escalate by one step rather than to the extreme. Still a violation: a plea is
# not a jury verdict, an investigation is not a charge, a settlement is not a loss.
_LEGAL_BORDERLINE = [
    (r"\bpleaded guilty to\b", "was convicted by a jury of"),
    (r"\bpleads guilty to\b", "was convicted by a jury of"),
    (r"\bunder investigation\b", "facing criminal charges"),
    (r"\bis investigating\b", "has charged"),
    (r"\bare investigating\b", "have charged"),
    (r"\binvestigation into\b", "criminal case against"),
    (r"\bsettled\b", "lost"),
    (r"\bunder scrutiny\b", "facing criminal charges"),
]

# Delete the hedge and change nothing else. The realistic failure mode.
#
# The third element is the word the ARTICLE must itself carry. Deleting a hedge
# is only an over-characterization if the source hedged: a summary that wrote
# "reportedly" off its own bat, about an article that states the fact plainly,
# is not made unfaithful by removing it. One of the first seven quiet plants was
# exactly that — same class of unsound ground truth as the RTVE case, caught by
# checking rather than assuming.
_LEGAL_QUIET = [
    (r"\balleged\s+", "", r"alleged"),
    (r"\ballegedly\s+", "", r"allegedly"),
    (r"\bsuspected\s+", "", r"suspected"),
    (r"\breportedly\s+", "", r"reportedly"),
    (r"\bapparent\s+", "", r"apparent"),
]


_VOWEL_START = re.compile(r"^[aeiouAEIOU]")
_PRONOUN = re.compile(r"\b(?:he|she|his|her|hers|him|they|them|their|theirs)\b", re.I)


def _fix_articles(text: str) -> str:
    """Repair a/an agreement after a word is deleted.

    Deleting "alleged" from "an alleged large-scale ring" leaves "an
    large-scale" — ungrammatical, and a cue the judge could react to that has
    nothing to do with the planted violation. The plant must differ from the
    original ONLY in the characteristic being tested.
    """
    def fix(m: re.Match) -> str:
        art, nxt = m.group(1), m.group(2)
        want = "an" if _VOWEL_START.match(nxt) else "a"
        if art[0].isupper():
            want = want.capitalize()
        return f"{want} {nxt}"
    return re.sub(r"\b([Aa]n?)\s+(\w+)", fix, text)


def _orphans_pronoun(original: str, edited: str, removed: str) -> bool:
    """Did the edit delete the antecedent a later pronoun depends on?

    Stripping "President Trump said" from "President Trump said X and he will
    announce his position" leaves "he"/"his" pointing at nothing. That is a
    broken sentence rather than a flat assertion, so the judge might object for
    a reason unrelated to attribution.
    """
    if not _PRONOUN.search(edited):
        return False
    # A proper noun was removed, and the remainder still leans on a pronoun.
    return bool(re.search(r"\b[A-Z][a-z]+", removed)) and not re.search(
        r"\b[A-Z][a-z]+", edited.split(_PRONOUN.search(edited).group(0))[0])


@dataclass(frozen=True)
class Plant:
    """One corrupted summary, and the provenance that makes it ground truth."""
    axis: str
    tier: str
    rule: str
    title: str
    article: str
    summary: str


def plant_supported(title: str, article: str, summary: str) -> list[Plant]:
    """Append a fabricated figure — only if it is genuinely absent upstream."""
    if _FABRICATED_MARK in article or _FABRICATED_MARK in summary:
        return []
    return [Plant("supported", "loud", "fabricated-figure", title, article,
                  summary.rstrip() + " " + _FABRICATED)]


def _apply(rules, summary: str) -> list[tuple[str, str]]:
    """Every rule that fires, not just the first.

    First-match-wins discarded most of an already-small sample: a summary
    naming both an investigation and a charge yielded one plant instead of two.
    """
    out = []
    for pattern, replacement in rules:
        if re.search(pattern, summary, re.I):
            edited = re.sub(pattern, replacement, summary, count=1, flags=re.I)
            edited = _fix_articles(re.sub(r"\s{2,}", " ", edited).strip())
            if edited and edited != summary.strip():
                out.append((edited, pattern))
    return out


def plant_legal(title: str, article: str, summary: str) -> list[Plant]:
    """Escalate a legal characterization — only where a legal matter exists.

    The article gate is the correction described at the top of this file: the
    axis asks about legal matters, so a summary with no legal matter behind it
    cannot host a `legal_safe` violation.
    """
    if not has_legal_context(article):
        return []
    out = []
    body = _plain(article)
    for tier, rules in (("loud", _LEGAL_LOUD),
                        ("borderline", _LEGAL_BORDERLINE)):
        for edited, pattern in _apply(rules, summary):
            out.append(Plant("legal_safe", tier, pattern, title, article, edited))
    for pattern, replacement, needs in _LEGAL_QUIET:
        if not re.search(rf"\b{needs}\b", body, re.I):
            continue
        for edited, _ in _apply([(pattern, replacement)], summary):
            out.append(Plant("legal_safe", "quiet", pattern, title, article, edited))
    return out


def plant_attribution(title: str, article: str, summary: str) -> list[Plant]:
    """Remove the summary's attribution — only where the article attributed it."""
    out = []
    for pattern in _ATTRIBUTIONS:
        m = re.search(pattern, summary, re.I)
        if not m:
            continue
        attributor = m.group(1) if m.groups() else ""
        if not _attributes(article, attributor):
            continue
        removed = m.group(0)
        # loud: strip the clause entirely, leaving a flat assertion.
        stripped = re.sub(pattern, "", summary, count=1, flags=re.I).strip()
        if (stripped and stripped != summary.strip()
                and not _orphans_pronoun(summary, stripped, removed)):
            out.append(Plant("attributed", "loud", pattern, title, article,
                             stripped[0].upper() + stripped[1:]))
        # quiet: keep a hedge but delete the source, so the claim is no longer
        # traceable to whoever made it.
        vague = re.sub(pattern, "reportedly ", summary, count=1, flags=re.I).strip()
        vague = re.sub(r"\s{2,}", " ", vague)
        if (vague and vague != summary.strip()
                and not _orphans_pronoun(summary, vague, removed)):
            out.append(Plant("attributed", "quiet", pattern, title, article,
                             vague[0].upper() + vague[1:]))
        break
    return out


def make_control(title: str, article: str, summary: str, source_name: str) -> Plant:
    """A strictly MORE hedged summary. Not a violation, by construction.

    Hedging harder cannot over-characterize a legal matter or drop an
    attribution, so `legal_safe` and `attributed` must both come back true.
    Anything else is a false positive on the population that matters.
    """
    body = summary.strip()
    return Plant("control", "control", "attributed-wrapper", title, article,
                 f"According to {source_name}, {body[0].lower()}{body[1:]}")


PLANTS = {
    "supported": plant_supported,
    "legal_safe": plant_legal,
    "attributed": plant_attribution,
}


# ── running the judge ────────────────────────────────────────────────────

async def judge_many(sem, plant: Plant, repeats: int, rubric: str) -> dict | None:
    """Judge one item `repeats` times and take the majority per axis.

    A single draw cannot tell a genuine miss from a coin flip. The original run
    reported `attributed 0/1` off one call on one item — a result that could
    have come back the other way on a re-run, with nothing in the output to say
    so. `unanimous` records whether the judge actually agreed with itself here.
    """
    verdicts = await asyncio.gather(*(
        judge_one(sem, plant.title, plant.article, plant.summary, rubric)
        for _ in range(repeats)
    ))
    got = [v for v in verdicts if v]
    if not got:
        return None
    out = {"n_draws": len(got)}
    for axis in AXES:
        votes = Counter(bool(v[axis]) for v in got)
        out[axis] = votes.most_common(1)[0][0]
        out[f"{axis}_unanimous"] = len(votes) == 1
    return out


def _pct(num: int, den: int) -> str:
    return f"{num:3d}/{den:<3d} ({num / den:5.1%})" if den else f"{'—':>3}/0   (  n/a)"


async def main(corpus_paths: list[Path], runs_paths: list[Path], min_body: int,
               limit: int, concurrency: int, repeats: int, variant: str,
               axes: tuple[str, ...]) -> int:
    # Several corpora pool into one sample, deduped by source_url. A single
    # fetch holds ~10 entries per feed, so one capture cannot supply enough
    # summaries that touch a legal matter to calibrate on — the engaged subset
    # is ~25% of the eligible subset, which is ~29% of the fetch.
    corpus = [a for path in corpus_paths for a in load_corpus(path)]
    by_url = {a.source_url: a for a in corpus}
    incumbent: dict[str, dict] = {}
    for path in runs_paths:
        incumbent.update(json.loads(path.read_text())["runs"][0])
    rubric = build_rubric(variant)

    items = [
        (by_url[u].title, by_url[u].raw_content, incumbent[u]["summary"],
         by_url[u].source_name)
        for u in by_url
        if u in incumbent and len((by_url[u].raw_content or "").split()) >= min_body
    ]
    # A fixed default silently truncated a pooled corpus — 155 eligible items
    # were judged as 150 with nothing in the output saying so. 0 means all.
    if limit:
        if limit < len(items):
            print(f"  NOTE: --limit {limit} drops {len(items) - limit} eligible items")
        items = items[:limit]
    if not items:
        raise SystemExit(
            f"No articles with {min_body}+ body words and a recorded summary. "
            f"Re-run --sample and --self-agreement --save-runs first."
        )

    # With a subset of axes selected, only items that ENGAGE one of them are
    # worth judging — that is the conditioned population anyway, and it keeps a
    # rubric A/B from re-paying for the `supported` axis it cannot move.
    if set(axes) != set(AXES):
        items = [it for it in items
                 if (("legal_safe" in axes and has_legal_context(it[1]))
                     or ("attributed" in axes and plant_attribution(it[0], it[1], it[2]))
                     or "supported" in axes)]
    legal_on = [it for it in items if has_legal_context(it[1])]
    print(f"  {len(items)} articles with {min_body}+ words of body text")
    print(f"  rubric: {variant}   repeats: {repeats}")
    print(f"  {len(legal_on)} of {len(items)} carry a legal matter in the ARTICLE "
          f"({len(legal_on) / len(items):.0%})\n")

    sem = asyncio.Semaphore(concurrency)

    # ── sensitivity, graded by how loud the violation is ──
    print("  SENSITIVITY — planted violations, each true by construction")
    print(f"    {'axis':12s} {'tier':11s} {'detected':16s} unanimous")
    detection: dict[tuple[str, str], tuple[int, int]] = {}
    for axis, planter in PLANTS.items():
        if axis not in axes:
            continue
        plants = [p for ti, a, s, _ in items for p in planter(ti, a, s)]
        if not plants:
            print(f"    {axis:12s} {'—':11s} no item engaged this axis")
            continue
        for tier in TIERS:
            tier_plants = [p for p in plants if p.tier == tier]
            if not tier_plants:
                continue
            verdicts = await asyncio.gather(
                *(judge_many(sem, p, repeats, rubric) for p in tier_plants))
            got = [v for v in verdicts if v]
            if not got:
                continue
            caught = sum(1 for v in got if not v[axis])
            unan = sum(1 for v in got if v[f"{axis}_unanimous"])
            detection[(axis, tier)] = (caught, len(got))
            print(f"    {axis:12s} {tier:11s} {_pct(caught, len(got)):16s} "
                  f"{_pct(unan, len(got))}")

    # ── specificity, split by whether the axis was even engaged ──
    #
    # Pooled, `legal_safe` reads ~1.000 on any corpus, because the rubric says
    # "true if no legal matter is involved" and most summaries have none. That
    # is a vacuous true, not a clean bill of health, and pooling hides which
    # one you are looking at.
    print("\n  SPECIFICITY — unmodified summaries, judged as-is")
    clean = await asyncio.gather(
        *(judge_many(sem, Plant("clean", "clean", "-", ti, a, s), repeats, rubric)
          for ti, a, s, _ in items))
    paired = [(it, v) for it, v in zip(items, clean, strict=True) if v]
    engaged_flags = {
        "supported": [True] * len(paired),
        "legal_safe": [has_legal_context(it[1]) for it, _ in paired],
        "attributed": [bool(plant_attribution(it[0], it[1], it[2])) for it, _ in paired],
    }
    print(f"    {'axis':12s} {'flagged (all)':17s} {'flagged (engaged)':18s} vacuous-true")
    for axis in axes:
        vs = [v for _, v in paired]
        flagged_all = sum(1 for v in vs if not v[axis])
        eng = [v for v, on in zip(vs, engaged_flags[axis], strict=True) if on]
        flagged_eng = sum(1 for v in eng if not v[axis])
        vacuous = sum(1 for v, on in zip(vs, engaged_flags[axis], strict=True)
                      if not on and v[axis])
        print(f"    {axis:12s} {_pct(flagged_all, len(vs)):17s} "
              f"{_pct(flagged_eng, len(eng)):18s} {_pct(vacuous, len(vs))}")

    # ── negative controls: strictly more hedged, so never a violation ──
    print("\n  NEGATIVE CONTROLS — strictly more hedged, must NOT be flagged")
    controls = [make_control(ti, a, s, src) for ti, a, s, src in items
                if has_legal_context(a)]
    ctrl_flagged: dict[str, tuple[int, int]] = {}
    if controls:
        cv = await asyncio.gather(
            *(judge_many(sem, c, repeats, rubric) for c in controls))
        got = [v for v in cv if v]
        for axis in ("legal_safe", "attributed"):
            bad = sum(1 for v in got if not v[axis])
            ctrl_flagged[axis] = (bad, len(got))
            print(f"    {axis:12s} falsely flagged {_pct(bad, len(got))}")
    else:
        print("    none — no item carried a legal matter")

    # ── verdict ──
    print("\n  VERDICT")
    ok = True
    for axis in [a for a in ("legal_safe", "attributed") if a in axes]:
        tiers = {t: detection[(axis, t)] for t in TIERS if (axis, t) in detection}
        if not tiers:
            print(f"    {axis}: NOT CALIBRATED — nothing engaged this axis.")
            ok = False
            continue
        total_n = sum(n for _, n in tiers.values())
        if total_n < 5:
            print(f"    {axis}: UNDERPOWERED — {total_n} planted case(s). A "
                  f"detection rate here is not a rate.")
            ok = False
        weak = [t for t, (c, n) in tiers.items() if n and c / n < 0.80]
        if weak:
            print(f"    {axis}: detection below 80% on {', '.join(weak)}. The "
                  f"judge misses violations it is shown, so a pass rate on this "
                  f"axis says nothing.")
            ok = False
        bad, n = ctrl_flagged.get(axis, (0, 0))
        if n and bad / n > 0.10:
            print(f"    {axis}: flags {bad}/{n} strictly-more-hedged controls. "
                  f"It objects to text that cannot be a violation.")
            ok = False
    if ok:
        print("    Both axes detect planted violations at every tier present,")
        print("    and pass the more-hedged controls. A rate measured with this")
        print("    judge means what it says.")
    print("\n    Detection on `loud` alone is a FLOOR, not a rate: the plants")
    print("    escalate, and production is likelier to drop a hedge quietly.")
    print("    Read the `quiet` row before trusting any headline number.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, nargs="+", default=[DEFAULT_CORPUS])
    p.add_argument("--runs", type=Path, nargs="+", default=[DEFAULT_RUNS])
    p.add_argument("--min-body-words", type=int, default=DEFAULT_MIN_BODY_WORDS)
    p.add_argument("--limit", type=int, default=0,
                   help="cap eligible items (0 = no cap)")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--repeats", type=int, default=3,
                   help="judge each item N times and take the majority")
    p.add_argument("--rubric", choices=sorted(LEGAL_AXES), default="as-shipped",
                   help="which reading of the `legal_safe` axis to score")
    p.add_argument("--axes", nargs="+", choices=AXES, default=list(AXES),
                   help="restrict which axes are planted and reported")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.corpus, a.runs, a.min_body_words, a.limit,
                              a.concurrency, a.repeats, a.rubric,
                              tuple(a.axes))))
