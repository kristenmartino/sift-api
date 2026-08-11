from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.story_synthesizer")

MODEL = "claude-haiku-4-5-20251001"


async def synthesize_story(
    articles: list[dict],
    *,
    client: anthropic.AsyncAnthropic | None = None,
    model: str = MODEL,
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

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += (
            f"\n{i}. [{article['source_name']}] \"{article['title']}\"\n"
            f"   {article['summary']}\n"
        )

    prompt = f"""These {len(articles)} articles from different news sources cover the same event:
{articles_text}

Generate:
1. headline: A unified headline capturing the core event. Be neutral and factual, not biased toward any single source.
2. summary: A 2-3 sentence synthesis combining the most important facts from all sources.
3. framings: For each source, provide:
   - source_name: exact name as given above
   - framing: One sentence describing this outlet's angle or emphasis
   - tone: One of "neutral", "urgent", "analytical", "critical", "optimistic"

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
        log_usage("story_synthesizer.synthesize", response, model=model)

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
            # Validate framings
            framings = result.get("framings", [])
            valid_tones = {"neutral", "urgent", "analytical", "critical", "optimistic"}
            for f in framings:
                if f.get("tone") not in valid_tones:
                    f["tone"] = "neutral"
            result["framings"] = framings
            return result

        logger.warning("Synthesis returned incomplete JSON, using fallback")
        return _fallback(articles)
    except Exception as e:
        logger.error("Story synthesis failed: %s", e)
        return _fallback(articles)


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
        "summary": articles[0].get("summary", ""),
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
