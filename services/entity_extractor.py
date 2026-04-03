from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings

logger = logging.getLogger("sift-api.entity_extractor")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 15  # More articles per call since extraction is lighter than summarization


async def extract_entities(articles: list[dict]) -> dict[str, dict]:
    """
    Batch entity extraction via Claude Haiku.

    Input: list of dicts with keys: source_url, title, summary, source_name
    Output: dict mapping source_url -> {people, organizations, locations, event_description}
    """
    if not articles:
        return {}

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    results: dict[str, dict] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            batch_results = await _extract_batch(client, batch)
            results.update(batch_results)
        except Exception as e:
            logger.error("Entity extraction failed for batch %d: %s", i // BATCH_SIZE, e)
            for article in batch:
                results[article["source_url"]] = _empty_entities()

    logger.info("Extracted entities for %d/%d articles", len(results), len(articles))
    return results


async def _extract_batch(
    client: anthropic.AsyncAnthropic,
    batch: list[dict],
) -> dict[str, dict]:
    """Send a batch of articles to Claude Haiku for entity extraction."""
    articles_text = ""
    for i, article in enumerate(batch, 1):
        articles_text += (
            f"\n{i}. [{article['source_name']}] \"{article['title']}\"\n"
            f"   Summary: {article['summary']}\n"
        )

    prompt = f"""Extract named entities from each of the following news articles.

For each article, return:
- people: list of people mentioned by name
- organizations: list of companies, governments, agencies, or bodies mentioned
- locations: list of specific places, countries, or regions mentioned
- event_description: a brief phrase (5-10 words) describing the core event

{articles_text}

Return a JSON array with one object per article, in the same order:
[{{"index": 1, "people": ["Name1"], "organizations": ["Org1"], "locations": ["Place1"], "event_description": "brief event phrase"}}, ...]

Return ONLY the JSON array, no other text."""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_entities(text, batch)


def _parse_entities(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's entity extraction response."""
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if parsed:
        for item in parsed:
            idx = item.get("index")
            if isinstance(idx, int) and 1 <= idx <= len(batch):
                results[batch[idx - 1]["source_url"]] = {
                    "people": item.get("people", []),
                    "organizations": item.get("organizations", []),
                    "locations": item.get("locations", []),
                    "event_description": item.get("event_description", ""),
                }
    else:
        logger.warning("Failed to parse entity extraction JSON")
        for article in batch:
            results[article["source_url"]] = _empty_entities()

    return results


def _empty_entities() -> dict:
    return {"people": [], "organizations": [], "locations": [], "event_description": ""}


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
