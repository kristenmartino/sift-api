from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.story_synthesizer")

MODEL = "claude-haiku-4-5-20251001"


async def synthesize_story(articles: list[dict]) -> dict:
    """
    Generate unified headline, summary, and per-source framings for a story cluster.

    Input: list of dicts with keys: title, summary, source_name, source_url
    Output: {headline, summary, framings: [{source_name, framing, tone}]}
    """
    if len(articles) < 2:
        return _fallback(articles)

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
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        log_usage("story_synthesizer.synthesize", response, model=MODEL)

        text = "".join(b.text for b in response.content if b.type == "text")
        result = _extract_json_object(text)

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
