"""LLM-judge for AI-generated card copy (sift-api#90).

This is the OFFLINE measurement half of the quality gate. It scores each
generated `why_it_matters` line (or `background`) on the three axes the issue
asks for:

  (a) restates    — does the line restate/paraphrase the summary (no new fact)?
  (b) adds_significance — does it add a concrete, VERIFIABLE significance not in
       the title/summary?
  (c) neutral_cliche_free — is it neutral and free of editorializing/clichés?

A line PASSES iff: not restates AND adds_significance AND neutral_cliche_free.

It is NOT wired into the production pipeline — the rubric in the generation
prompt plus services/quality_gate.py do the runtime work for free. The judge is
run by scripts/eval_why_it_matters.py to baseline against the sift#150 audit
corpus and prove the rubric moved the restatement + cliché rates. A judge call
costs a real model invocation, so it stays opt-in and out of the hot path.

Uses Sonnet (not the Haiku used for generation): the judge needs stronger
semantic discrimination than the thing it audits, and offline eval is not cost-
sensitive the way per-article ingest is.
"""
from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.judge")

JUDGE_MODEL = "claude-sonnet-4-6"
JUDGE_BATCH_SIZE = 10

# Per-field framing. why_it_matters is judged on all three axes; background is a
# context paragraph (expected to share topic vocabulary), so restatement is not
# penalized — only neutrality/cliché.
_FIELD_RUBRIC = {
    "why_it_matters": (
        "Each LINE is a one-sentence \"why it matters\" note meant to give the "
        "reader a concrete, verifiable stake beyond the headline."
    ),
    "background": (
        "Each LINE is a short context paragraph meant to teach background the "
        "reader may be missing. It MAY reuse topic vocabulary — do NOT treat "
        "shared wording as restatement; judge it mainly on neutrality/clichés."
    ),
}


def _build_items_text(items: list[dict]) -> str:
    text = ""
    for i, it in enumerate(items, 1):
        text += (
            f"\n{i}. TITLE: {it.get('title', '')}\n"
            f"   SUMMARY: {it.get('summary', '')}\n"
            f"   LINE: {it.get('line', '')}\n"
        )
    return text


def build_judge_prompt(items: list[dict], field: str = "why_it_matters") -> str:
    framing = _FIELD_RUBRIC.get(field, _FIELD_RUBRIC["why_it_matters"])
    return f"""You are auditing AI-generated news-card copy. {framing}

For each item you are given the article TITLE, the SUMMARY, and the generated \
LINE. Judge ONLY the line, against the title + summary, on three independent \
boolean axes:

a) restates — TRUE if the line mostly restates or paraphrases the title/summary \
without adding a new fact. Rewording what's already said is restatement.
b) adds_significance — TRUE if the line adds a CONCRETE, VERIFIABLE significance \
NOT already in the title/summary: a specific consequence, who is affected, a \
number, a precedent, or what changes next. Vague importance ("this is a big \
deal", "people are paying attention") is NOT significance.
c) neutral_cliche_free — TRUE if the line is neutral (no opinion, no \
editorializing) AND free of vague-significance clichés ("raises serious \
questions", "a turning point", "could finally…", "a wake-up call", emotional \
color like "fans finally have hope").

A line PASSES only when restates=false AND adds_significance=true AND \
neutral_cliche_free=true. Otherwise it FAILS.

Items:
{_build_items_text(items)}

Return a JSON array with one object per item, in the same order. Use short keys:
i = index (1-based)
r = restates (boolean)
a = adds_significance (boolean)
n = neutral_cliche_free (boolean)
why = a reason of at most 12 words (string)

[{{"i":1,"r":false,"a":true,"n":true,"why":"adds the downstream price impact"}}, ...]

Return ONLY the JSON array, no other text."""


def _coerce_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "1"}:
            return True
        if v in {"false", "no", "0"}:
            return False
    return None


def _derive_verdict(restates: bool | None, adds: bool | None, neutral: bool | None) -> str:
    """A line passes only when it restates nothing, adds significance, and is
    neutral/cliché-free. Any unknown axis is treated as a fail (conservative)."""
    if restates is False and adds is True and neutral is True:
        return "pass"
    if None in (restates, adds, neutral):
        return "error"
    return "fail"


def _parse_judge(text: str, n_items: int) -> dict[int, dict]:
    """Parse the judge's JSON array into {1-based index -> axes dict}."""
    parsed = _extract_json_array(text)
    if not parsed:
        logger.warning("Failed to parse judge JSON")
        return {}

    out: dict[int, dict] = {}
    for item in parsed:
        idx = item.get("i", item.get("index"))
        if not (isinstance(idx, int) and 1 <= idx <= n_items):
            continue
        restates = _coerce_bool(item.get("r", item.get("restates")))
        adds = _coerce_bool(item.get("a", item.get("adds_significance")))
        neutral = _coerce_bool(item.get("n", item.get("neutral_cliche_free")))
        out[idx] = {
            "restates": restates,
            "adds_significance": adds,
            "neutral_cliche_free": neutral,
            "verdict": _derive_verdict(restates, adds, neutral),
            "reason": (item.get("why") or item.get("reason") or "").strip(),
        }
    return out


def _verdict_record(item: dict, axes: dict | None) -> dict:
    """Merge passthrough identity (id/source_url/field) with the judged axes.

    When the judge returned nothing for this item, mark verdict="error" so the
    eval counts it as unscored rather than silently passing or failing it.
    """
    base = {
        "id": item.get("id") or item.get("source_url"),
        "line": item.get("line", ""),
        "judged": axes is not None,
    }
    if axes is None:
        base.update({
            "restates": None,
            "adds_significance": None,
            "neutral_cliche_free": None,
            "verdict": "error",
            "reason": "no judge output",
        })
    else:
        base.update(axes)
    return base


async def judge_lines(
    items: list[dict],
    *,
    field: str = "why_it_matters",
    client: anthropic.AsyncAnthropic | None = None,
    model: str = JUDGE_MODEL,
) -> list[dict]:
    """Judge a list of {title, summary, line, [id]} items.

    Returns one verdict record per item, aligned by position. Optional reusable
    client so a long eval run shares one connection.
    """
    if not items:
        return []

    own_client = client is None
    if own_client:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    verdicts: list[dict | None] = [None] * len(items)
    for start in range(0, len(items), JUDGE_BATCH_SIZE):
        sub = items[start : start + JUDGE_BATCH_SIZE]
        axes_by_idx: dict[int, dict] = {}
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=1200,
                messages=[{"role": "user", "content": build_judge_prompt(sub, field)}],
            )
            log_usage("judge.batch", response, model=model)
            text = "".join(b.text for b in response.content if b.type == "text")
            axes_by_idx = _parse_judge(text, len(sub))
        except Exception as e:
            logger.error("judge batch failed at offset %d: %s", start, e)

        for j, it in enumerate(sub):
            verdicts[start + j] = _verdict_record(it, axes_by_idx.get(j + 1))

    return [v for v in verdicts if v is not None]


def judge_rejects(verdict: dict) -> bool:
    """Runtime-drop criterion for a why_it_matters line.

    Targets the residual the deterministic gate can't catch: paraphrased
    restatement and editorial color without a known cliché phrase. Drops on
    `restates` or non-neutrality — but deliberately NOT on `adds_significance`
    alone, the strictest and fuzziest axis, which would over-suppress (the judge
    passes only ~1/3 of post-rubric lines on it). An unscored verdict (judge
    error) is kept, never dropped.
    """
    if not verdict.get("judged"):
        return False
    return verdict.get("restates") is True or verdict.get("neutral_cliche_free") is False


def tally(verdicts: list[dict]) -> dict:
    """Aggregate judge verdicts into the audit-comparable rates.

    Rates are over the items the judge actually scored (judged=True); `errors`
    reports how many were unscored. Pure — used by the eval and the tests.
    """
    scored = [v for v in verdicts if v.get("judged")]
    n = len(scored)
    errors = len(verdicts) - n

    def rate(pred) -> float:
        return (sum(1 for v in scored if pred(v)) / n) if n else 0.0

    return {
        "n": n,
        "errors": errors,
        "pass_rate": rate(lambda v: v["verdict"] == "pass"),
        "restatement_rate": rate(lambda v: v.get("restates") is True),
        "cliche_or_editorial_rate": rate(lambda v: v.get("neutral_cliche_free") is False),
        "adds_significance_rate": rate(lambda v: v.get("adds_significance") is True),
    }


def _extract_json_array(text: str) -> list[dict] | None:
    """Extract a JSON array from LLM output, tolerating surrounding prose."""
    text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return None
