"""Focused semantic review for generated card copy.

The original significance evaluator remains available offline. Production uses
budget-checked term usefulness, background novelty and source-note novelty
reviews. Unknown/unavailable verdicts withhold optional copy. These checks do
not establish the factual truth of every source or define a universal quality score.
"""
from __future__ import annotations

from services.paid_calls import budgeted_message

import json
import logging

import anthropic

from app.config import settings
from services.index_alignment import AlignmentError, aligned_entries
from services.model_registry import resolve
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.judge")

OPERATION = "judge.batch"
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
            f"   GLOSSARY: {json.dumps(it.get('terms', []), ensure_ascii=False)}\n"
            f"   LINE: {it.get('line', '')}\n"
        )
    return text


def build_judge_prompt(items: list[dict], field: str = "why_it_matters") -> str:
    if field == "term_utility":
        return f"""Review each glossary entry (LINE) on a news card.
r=true if it defines ordinary vocabulary, merely expands/repeats the term,
or repeats an explanation already in TITLE or SUMMARY.
a=true only for domain-specific jargon, a technical mechanism, institutional
procedure or legal term of art that a college-educated reader may need explained.
n=true only for a neutral, precise definition without speculative significance
or unsupported procedural guarantees. Check factual precision as well as usefulness:
a definition must not confuse an entire geological period with its late subperiod,
guarantee that targeted cancer treatments spare healthy tissue, or overstate legal
coverage. If precision cannot be established, n=false. Default to omitting marginal entries.
Reject "stock low", "market value", "campaign", "poll" and "earnings report".
Retain useful explanations such as habeas corpus, cloture, basis points,
tripwire military deployment and denaturalization when present in the story.
Return ONLY one JSON entry per numbered item:
[{{"i":1,"r":true,"a":false,"n":true,"why":"ordinary vocabulary"}}]
{_build_items_text(items)}"""
    if field == "background_novelty":
        return f"""Judge whether each news-card background (LINE) earns its place.
TITLE and SUMMARY describe the news the reader has already read.
GLOSSARY lists the definitions already shown alongside the background.
Treat a paraphrase of any GLOSSARY definition as repetition too, even when
its wording differs. A paragraph defining tripwire followed by a tripwire
glossary definition repeats the same explanation: r=true, a=false.
r = true if the background only paraphrases that news, defines a familiar
proper name, or gives a general overview without teaching a specific mechanism,
institutional role, procedure or difficult concept beyond the summary.
a = true only when a useful specific explanation remains beyond that news.
n = true when the explanation avoids generic significance, filler and editorial
claims. Set n=false if the paragraph adds article-specific policy scope,
enforcement steps, venue restrictions, or procedural guarantees absent from
TITLE/SUMMARY. A generic institutional role can be useful; a guessed condition
on this specific news event cannot. For example, "Cornell bans frat parties"
does not support a paragraph claiming a campus-only scope or facility-use rules.
Apply the same usefulness bar as the glossary. A paragraph defining familiar
ordinary vocabulary (a runoff election, trade deadline or red-light camera) is still an obvious
definition even when that term has no glossary entry: r=true, a=false. Do not
approve a rejected glossary definition merely because it was moved into prose.
Do not approve categorical acquisition or postseason-eligibility guarantees
based only on a trade deadline. Those rules have exceptions and require an
authoritative reference; set n=false when no such support is supplied.
A useful institutional role (such as the DNI) or an unfamiliar procedural
explanation can still pass. Do not invent a source's legal basis or an employee's appointment class. A federal
prosecutor is not automatically a Senate-confirmed U.S. Attorney or presidential
appointee. Named-law claims need supplied support. Check precision, not just plausible tone:
reject an unsourced event history, compulsory league membership rule, financial
coverage guarantee, religious practice, benchmark publisher, or court's powers.
Do not approve a mechanism merely because it sounds educational. If precision
cannot be established, n=false. Stable roles can still pass. A paragraph must
not contradict the supplied election result (e.g. claiming a first-round win
when SUMMARY says the candidate reversed a first-round defeat).
Shared topic vocabulary alone is not repetition.

Example: a summary already calls Impulso Morelia a post-production section of
the film festival and states its funding award. Explaining that this section
gives filmmakers funding to finish and promote films repeats that news: r=true,
a=false. By contrast, explaining what the Director of National Intelligence
oversees when a summary only names the DNI's memo adds an institutional role:
r=false, a=true.

Return ONLY a JSON array with one entry per numbered item:
[{{"i":1,"r":true,"a":false,"n":true,"why":"repeats the funding award context"}}]
Use booleans and a reason of at most 12 words.
{_build_items_text(items)}"""
    if field == "framing":
        return f"""Compare each source NOTE (LINE) with the shared story TITLE and SUMMARY.
The note is already an exact passage from a supplied source. Your task is only
to decide whether it adds a specific detail beyond the shared story.

r = true when it repeats or paraphrases information already in the shared story,
even using different words. 'Experts assess human-to-human transmission risk as
low' and 'Researchers say the risk of spreading between people is low' repeat
the same finding. Merely adding 'explicitly states', a source name, or a synonym
does not add a fact. A distinct figure, qualification, attributed claim,
document or procedural detail can add information; do not reject it just for
sharing topic vocabulary.
a = true only if a concrete detail remains beyond the shared story.
n = true if the note avoids generic significance or an invented outlet angle.
Set n=false for a note describing an outdated state contradicted by the shared
summary, such as "no arrests" beside a later arrest, unless the note explicitly
identifies the earlier time. A contradiction is not automatically a useful angle.

Return ONLY a JSON array, exactly one entry per numbered item:
[{{"i":1,"r":true,"a":false,"n":true,"why":"repeats the shared low-risk assessment"}}]
Use booleans, with a reason of at most 12 words.

Items:
{_build_items_text(items)}"""
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
    """Parse the judge's JSON array into {1-based index -> axes dict}.

    Same alignment rule as the generation paths (services/index_alignment.py):
    a duplicate, missing, or out-of-range index means no verdict can be trusted
    to belong to the line it would be applied to, so the whole response is
    discarded. Every item then comes back unscored — identical to the
    unparseable-response path above, which callers already handle by keeping
    the deterministic result (see judge_rejects on verdict="error").

    The stakes differ from the summarizer's: a shifted verdict does not put
    wrong text in front of a reader, it NULLs a good why_it_matters line and
    keeps a bad one. Still wrong, and cheaper to refuse than to detect later.
    """
    parsed = _extract_json_array(text)
    if not parsed:
        logger.warning("Failed to parse judge JSON")
        return {}

    try:
        entries = aligned_entries(parsed, n_items)
    except AlignmentError as e:
        logger.warning("Judge response does not line up with its items (%s); discarding", e)
        return {}

    out: dict[int, dict] = {}
    for idx, item in entries.items():
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
    # None, not a default of JUDGE_MODEL — a default argument is evaluated once
    # at import, which froze the model for the life of the process.
    model: str | None = None,
) -> list[dict]:
    """Judge a list of {title, summary, line, [id]} items.

    Returns one verdict record per item, aligned by position. Optional reusable
    client so a long eval run shares one connection.
    """
    if not items:
        return []

    model = model or resolve(OPERATION).model

    own_client = client is None
    if own_client:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    verdicts: list[dict | None] = [None] * len(items)
    for start in range(0, len(items), JUDGE_BATCH_SIZE):
        sub = items[start : start + JUDGE_BATCH_SIZE]
        axes_by_idx: dict[int, dict] = {}
        try:
            response = await budgeted_message(client, OPERATION,
                model=model,
                max_tokens=1200,
                messages=[{"role": "user", "content": build_judge_prompt(sub, field)}],
            )
            log_usage(OPERATION, response, model=model)
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
