from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings

logger = logging.getLogger("sift-api.context_generator")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 10


async def generate_context(articles: list[dict]) -> dict[str, dict]:
    """
    Batch-generate 'why it matters' one-liners and importance scores via Claude Haiku.

    Input: list of dicts with keys: source_url, title, summary
    Output: dict mapping source_url -> {"context": str, "score": int}
    """
    if not articles:
        return {}

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    results: dict[str, dict] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            batch_results = await _generate_batch(client, batch)
            results.update(batch_results)
        except Exception as e:
            logger.error("Context generation failed for batch %d: %s", i // BATCH_SIZE, e)

    logger.info("Generated context for %d/%d articles", len(results), len(articles))
    return results


async def _generate_batch(
    client: anthropic.AsyncAnthropic,
    batch: list[dict],
) -> dict[str, dict]:
    """Send a batch of articles to Claude Haiku for context + importance generation."""
    articles_text = ""
    for i, article in enumerate(batch, 1):
        articles_text += (
            f"\n{i}. \"{article['title']}\"\n"
            f"   Summary: {article['summary']}\n"
        )

    prompt = f"""For each article below, provide two things:
1. ONE sentence (max 18 words) explaining why a general reader should care. Be direct and conversational — like a smart friend giving context. Vary your sentence openings. Never start with "This matters because". Focus on real-world impact.
2. An importance score from 1-5:
   1 = routine/minor (local interest, incremental update)
   2 = somewhat notable (industry-specific, modest impact)
   3 = noteworthy (broad interest, clear significance)
   4 = significant (wide impact, affects many people)
   5 = breaking/major (historic, urgent, massive consequence)

Articles:
{articles_text}

Return a JSON array with one object per article, in the same order:
[{{"index": 1, "context": "Your one-liner here.", "score": 3}}, ...]

Return ONLY the JSON array, no other text."""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_context(text, batch)


def _parse_context(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's context + score response."""
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if parsed:
        for item in parsed:
            idx = item.get("index")
            context = item.get("context", "")
            score = item.get("score", 3)
            if isinstance(idx, int) and 1 <= idx <= len(batch) and context:
                # Clamp score to 1-5
                if not isinstance(score, int) or score < 1 or score > 5:
                    score = 3
                results[batch[idx - 1]["source_url"]] = {
                    "context": context,
                    "score": score,
                }
    else:
        logger.warning("Failed to parse context generation JSON")

    return results


def _extract_json_array(text: str) -> list[dict] | None:
    """Extract a JSON array from LLM output."""
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
