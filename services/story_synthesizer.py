from __future__ import annotations

import json
import logging
import re

import anthropic

from app.config import settings
from services.model_registry import resolve
from services.copy_rules import COPY_RULES, COPY_RULES_VERSION
from services.judge import judge_lines
from services.source_review import assess_copy
from services.cost_guard import check_budget
from services.cost_estimates import estimate_cost
from services.quality_gate import clean_generated_headline, gate_framing, gate_summary, unsupported_figures, takeover_role_conflict, interim_result_conflict, unsupported_name_change
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.story_synthesizer")

OPERATION = "story_synthesizer.synthesize"


async def synthesize_story(
    articles: list[dict],
    *,
    client: anthropic.AsyncAnthropic | None = None,
    # None, not a default of MODEL: a default argument is evaluated once at
    # import, so the old form froze the model for the life of the process and
    # an override could not take effect without a restart. Callers passing an
    # explicit model (eval scripts) still win.
    model: str | None = None,
) -> dict:
    """
    Generate unified headline, summary, and per-source framings for a story cluster.

    Input: list of dicts with keys: title, summary, source_name, source_url
    Output: {headline, summary, framings: [{source_name, framing, tone}]}

    Optional reusable client so an eval run shares one connection and can replay
    a recorded response. Mirrors services.judge.judge_lines.
    """
    if len(articles) < 2:
        return _fallback(articles)

    model = model or resolve(OPERATION).model

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += (
            f"\n{i}. [{article['source_name']}] \"{article['title']}\"\n"
            f"   {article['summary']}\n"
        )

    unresolved = bool(re.search(r"\bwhether\b.{0,120}\b(?:caused|contributed)\b", articles_text, re.I))
    causality_rule = (
        "The supplied reports explicitly leave causality unresolved. Describe the event and later outcome separately in the headline. "
        "Do not call a death 'linked to', 'from', or 'caused by' the event. The summary must explicitly retain the unresolved cause."
        if unresolved else ""
    )
    prompt = f"""{COPY_RULES}
{causality_rule}

These {len(articles)} articles were grouped automatically. Use only the shared event supported by their supplied text; do not invent a common event or causal link between merely related reports:
{articles_text}

Generate:
1. headline: A unified headline capturing the core event. Be neutral and factual, not biased toward any single source.
2. summary: At most two concise factual sentences combining the supported facts. One useful sentence is enough. Do not append a general observation about significance, broader tensions or implications, or commentary about how sources differ.
3. framings: For each source, provide:
   - source_name: exact name as given above
   - framing: Copy a contiguous passage from this source's headline or summary \
that adds a specific detail beyond the shared summary. Maximum 25 words. \
Return an empty string when no distinct emphasis is supported by the input.
   - tone: One of "neutral", "urgent", "analytical", "critical", "optimistic"

Rules for framing notes:
- Compare the supplied headlines and summaries, not the outlet's reputation \
or political-lean rating. You have not read the full articles.
- Name the actual difference: a named document, a specific figure, an attributed \
claim, or a procedural explanation. "Frames it within broader tensions" and \
"contextualizes wider escalation concerns" do not identify a difference.
- Even if an input summary uses those phrases, do not recycle them as a framing \
note. "Reflects escalating tensions between the U.S. and Iran" still supplies \
no distinct reported detail; return "" for that outlet. A shorter list of real \
differences is better than making a general observation sound analytical.
- Keep each non-empty framing to 25 words or fewer. State the detail directly, \
without phrases such as "broader concerns", "within the context", or "highlights the importance".
- Do not give every outlet a different angle just to fill the list. Keep one \
entry per source, with framing "" and tone "neutral" when no difference is apparent.
- Do not assert that an outlet omitted a fact merely because its supplied \
summary does not mention it. Do not infer motive from word choice.
- "Does not specify", "without mentioning", and "omits" are not coverage \
differences unless the supplied text explicitly reports a verified omission. \
Blank the note instead. If every source supplies the same detail, blank the \
notes rather than paraphrasing the shared summary for each outlet.
- State a distinguishing reported detail directly. Avoid "emphasizes", \
"focuses on", or "highlights" when those words only introduce a restatement.
- In headlines, retain uncertainty once: use "reportedly" or "sources say", \
not "reportedly ... sources say". Do not repeat "court says" after "court rules".

Keep buyer and target roles unchanged. "A nears a takeover of B" means A would
buy B, not that B is acquiring A. Preserve preliminary talks as talks.

Rules about people and legal matters — these override everything above:
- Use only what the articles above state. Do not add facts, motives, or history \
that are not in the text you were given.
- Never characterize a legal outcome beyond what the sources literally say. \
"Charged" is not "guilty." "Settled" is not "found liable." An investigation is \
not a finding. An accusation is not a fact.
- Attribute contested claims to whoever made them: "prosecutors say," "the \
complaint alleges," "according to <outlet>."
- A campaign contribution is not an endorsement. A committee seat is not a \
position. A vote is not a belief.
- Where the sources disagree about a fact, say they disagree. Do not pick a \
winner in the unified headline or summary.
- "tone" and "framing" describe the OUTLET's coverage, never the truth of the \
underlying claim, and never the character of any person named in it.

Return ONLY a JSON object:
{{"headline": "...", "summary": "...", "framings": [{{"source_name": "...", "framing": "...", "tone": "..."}}]}}"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=_max_tokens_for(len(articles)),
            messages=[{"role": "user", "content": prompt}],
        )
        log_usage(OPERATION, response, model=model)

        text = "".join(b.text for b in response.content if b.type == "text")
        result = _extract_json_object(text)
        stop_reason = getattr(response, "stop_reason", None)

        # Structured so "the model returned something unparseable" is
        # distinguishable from "the response was cut off mid-JSON". Under the
        # old fixed ceiling both logged the same line, which is why a
        # deterministic truncation read as intermittent API flakiness for a
        # day. Same shape as `cluster_stats` in story_clusterer.
        logger.info(json.dumps({
            "event": "synthesis_stats",
            "n_articles": len(articles),
            "n_outlets": len({a.get("source_name") for a in articles}),
            "stop_reason": stop_reason,
            "parsed": result is not None,
            "max_tokens": _max_tokens_for(len(articles)),
        }))
        if stop_reason == "max_tokens":
            logger.warning(
                "Synthesis response hit the output ceiling (%d articles) — "
                "framings were likely lost to truncation",
                len(articles),
            )

        if result and "headline" in result and "summary" in result:
            result["headline"] = clean_generated_headline(result["headline"])
            result["summary"] = gate_summary(result["summary"])
            if not result["summary"]:
                return _fallback(articles)
            if interim_result_conflict(result["headline"] + " " + result["summary"]):
                logger.warning("Story copy turned a halftime lead into a completed win; leaving synthesis retryable")
                return _fallback(articles)
            supplied = " ".join(f"{a['title']} {a['summary']}" for a in articles)
            if unsupported_name_change(result["headline"] + " " + result["summary"], supplied):
                logger.warning("Story changed or expanded an unsupported name; leaving synthesis retryable")
                return _fallback(articles)
            if any(takeover_role_conflict(result["headline"] + " " + result["summary"], a["title"]) for a in articles):
                logger.warning("Story copy reversed supplied takeover roles; leaving synthesis retryable")
                return _fallback(articles)
            if unsupported_figures(result["headline"] + " " + result["summary"], supplied):
                logger.warning("Story copy added an unsupported figure; leaving synthesis retryable")
                return _fallback(articles)
            if unresolved and (
                re.search(r"\b(?:death|fatalit\w*)\b.{0,40}\b(?:linked to|from|caused by|due to)\b|\b(?:killed|fatal|deadly)\b", result["headline"], re.I)
                or not re.search(r"\bwhether\b.{0,120}\b(?:caused|contributed)\b|\bcause\b.{0,40}\b(?:unknown|unclear|unresolved)\b", result["summary"], re.I)
            ):
                logger.warning("Story copy resolved an explicitly uncertain cause; leaving synthesis retryable")
                return _fallback(articles)
            review = await assess_copy([{"id": "story", "title": "Supplied story reports",
                "source": articles_text, "copy": result["headline"] + "\n" + result["summary"]}], client=client)
            if review.get("story", {}).get("v") != "pass":
                logger.warning("Story failed source/content review; leaving synthesis retryable")
                return _fallback(articles)
            raw_framings = result.get("framings", [])
            result["framings"] = _gate_framings(raw_framings, articles, result["summary"])
            await _review_framings(result, client)
            logger.info(json.dumps({
                "event": "story_copy_gated", "copy_rules_version": COPY_RULES_VERSION,
                "source_rows": len(result["framings"]),
                "nonempty_notes": sum(bool(f["framing"]) for f in result["framings"]),
            }))
            return result

        logger.warning("Synthesis returned incomplete JSON, using fallback")
        return _fallback(articles)
    except Exception as e:
        logger.error("Story synthesis failed: %s", e)
        return _fallback(articles)


async def _review_framings(result: dict, client) -> None:
    """Only distinct notes may publish; unavailable review leaves blank rows.

    This is a redundancy check, not the contingent general factuality judge.
    Source grounding is enforced separately by exact-passage checks.
    """
    notes = [f for f in result["framings"] if f["framing"]]
    if not notes:
        return
    verdicts = []
    try:
        budget = await check_budget(estimate_cost("judge.batch", len(notes)))
        if budget.allowed:
            verdicts = await judge_lines([
                {"id": f["source_name"], "title": result["headline"], "summary": result["summary"], "line": f["framing"]}
                for f in notes
            ], field="framing", client=client)
        by_source = {v["id"]: v for v in verdicts}
    except Exception as e:
        logger.error("Story note review unavailable; withholding optional notes: %s", e)
        by_source = {}
    for f in notes:
        v = by_source.get(f["source_name"], {})
        if not (v.get("judged") and v.get("restates") is False and v.get("adds_significance") is True and v.get("neutral_cliche_free") is True):
            f.update(framing="", tone="neutral")


def _gate_framings(raw: object, articles: list[dict], summary: str) -> list[dict]:
    """Build rows from real inputs, not the model's invented/duplicate names.

Empty notes retain their source. A second entry for the same source can supply
the first valid note, but cannot inflate the row count or overwrite it.
"""
    rows = {a["source_name"]: {"source_name": a["source_name"], "framing": "", "tone": "neutral"}
            for a in articles}
    valid_tones = {"neutral", "urgent", "analytical", "critical", "optimistic"}
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("source_name"), str):
            continue
        if f["source_name"] not in rows:
            continue
        name = f["source_name"]
        if rows[name]["framing"]:
            continue
        source_text = " ".join(f"{a['title']} {a['summary']}" for a in articles if a["source_name"] == name)
        note = gate_framing(f.get("framing"), source_text=source_text, summary=summary)
        tone = f.get("tone")
        rows[name].update(framing=note, tone=tone if note and isinstance(tone, str) and tone in valid_tones else "neutral")
    return list(rows.values())


def _max_tokens_for(article_count: int) -> int:
    """Output ceiling for a synthesis call, scaled to the input size.

    Clustering emits indices; this response carries one `framings` entry per
    source — a sentence plus source_name and tone — so its output grows with
    the cluster, and a fixed ceiling breaks on exactly the stories worth the
    most. Measured 2026-08-11 against five prod stories a fixed 1024 was
    truncating (`stop_reason='max_tokens'` on four of five, all five parsing
    cleanly at 4096): the worst was **1,348 output tokens for 24 articles
    across 18 outlets**, ~61 per article. 120 doubles that.

    Budgeted per *article*, not per outlet: on larger clusters the model emits
    roughly one framing per article rather than per source (18 outlets
    produced 24 framings), so article count is the honest upper bound.

    `max_tokens` is a ceiling, not a spend commitment — billing is on tokens
    actually produced — so the headroom is close to free. The floor keeps every
    cluster at or above the old fixed value, so nothing gets less room than it
    had.
    """
    return max(1024, min(8192, 400 + 120 * article_count))


def _fallback(articles: list[dict]) -> dict:
    """Fallback when synthesis fails: use first article's data."""
    if not articles:
        return {"headline": "", "summary": "", "framings": [], "_failed": True}
    return {
        "headline": articles[0].get("title", ""),
        "summary": gate_summary(articles[0].get("summary", "")),
        "framings": [],
        "_failed": True,
    }


def _extract_json_object(text: str) -> dict | None:
    """Extract a JSON object from LLM output."""
    text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None
