from __future__ import annotations

import json
import logging
import re

import anthropic

from app.config import settings
from app.models import RSSArticle

logger = logging.getLogger("sift-api.summarizer")

BATCH_SIZE = 5
MODEL = "claude-haiku-4-5-20251001"

VALID_CATEGORIES = {"top", "technology", "business", "science", "energy", "world", "health", "politics", "sports", "entertainment"}


async def summarize_articles(articles: list[RSSArticle]) -> dict[str, dict]:
    """
    Summarize and classify articles in batches using Claude Haiku.
    Returns a dict mapping source_url to {"summary": str, "category": str}.
    """
    if not articles:
        return {}

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
    results: dict[str, dict] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            batch_results = await _summarize_batch(client, batch)
            results.update(batch_results)
        except Exception as e:
            logger.error("Summarization failed for batch %d: %s", i // BATCH_SIZE, e)
            # Fall back to raw content for this batch
            for article in batch:
                if article.raw_content:
                    results[article.source_url] = {
                        "summary": _truncate(article.raw_content, 200),
                        "category": "top",
                    }

    logger.info("Summarized %d/%d articles", len(results), len(articles))
    return results


async def _summarize_batch(
    client: anthropic.AsyncAnthropic,
    batch: list[RSSArticle],
) -> dict[str, dict]:
    """Send a batch of articles to Claude Haiku and parse summaries + categories."""
    prompt = _build_prompt(batch)

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    return _parse_summaries(text, batch)


def _build_prompt(batch: list[RSSArticle]) -> str:
    """Build the summarization + classification prompt for a batch of articles."""
    articles_text = ""
    for i, article in enumerate(batch, 1):
        content = article.raw_content or article.title
        # Strip HTML tags from RSS content
        content = re.sub(r"<[^>]+>", "", content).strip()
        content = _truncate(content, 500)
        articles_text += f"\n{i}. Title: {article.title}\n   Source: {article.source_name}\n   Content: {content}\n"

    return f"""Summarize each of the following news articles in 1-2 concise sentences. Focus on the key facts and why the story matters.

Also classify each article into exactly ONE category:
- "top" — only for major breaking news or cross-cutting stories that transcend a single topic
- "technology" — tech industry, software, hardware, AI, cybersecurity, social media
- "business" — Wall Street, stock market, earnings reports, M&A, IPOs, venture capital, interest rates, Federal Reserve, banking, employment data, GDP, inflation, corporate strategy, trade policy. NOT consumer product launches, pop culture brands, or retail sales events
- "science" — research, discoveries, space, physics, biology, climate science
- "energy" — power grid, renewables, oil & gas, EVs, energy policy, utilities
- "world" — international affairs, geopolitics, diplomacy, foreign policy
- "health" — medicine, public health, pharma, healthcare policy, disease
- "politics" — elections, legislation, political parties, Congress, campaigns, government policy
- "sports" — professional sports, college sports, Olympics, player trades, game results
- "entertainment" — movies, TV, music, celebrities, streaming, awards, pop culture, consumer product launches, brand collaborations, viral consumer trends

Most articles should go into a specific topic category. Only use "top" for truly major stories.

{articles_text}

Return a JSON array with one object per article, in the same order:
[{{"index": 1, "summary": "1-2 sentence summary", "category": "technology"}}, ...]

Return ONLY the JSON array, no other text."""


def _parse_summaries(text: str, batch: list[RSSArticle]) -> dict[str, dict]:
    """Parse Claude's response into a url -> {summary, category} mapping."""
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if parsed:
        for item in parsed:
            idx = item.get("index")
            summary = item.get("summary", "")
            category = item.get("category", "top")
            if category not in VALID_CATEGORIES:
                category = "top"
            if isinstance(idx, int) and 1 <= idx <= len(batch) and summary:
                results[batch[idx - 1].source_url] = {
                    "summary": summary,
                    "category": category,
                }
    else:
        logger.warning("Failed to parse summary JSON, using raw text fallback")
        lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
        for i, line in enumerate(lines[: len(batch)]):
            line = re.sub(r"^\d+[\.\):\-]\s*", "", line)
            if line:
                results[batch[i].source_url] = {
                    "summary": line,
                    "category": "top",
                }

    return results


def _extract_json_array(text: str) -> list[dict] | None:
    """Extract a JSON array from potentially messy LLM output."""
    text = text.strip()

    # Strategy 1: direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: find [...] brackets
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # Strategy 3: find individual objects
    objects = re.findall(r"\{[^{}]*\}", text)
    if objects:
        items = []
        for obj_str in objects:
            try:
                items.append(json.loads(obj_str))
            except json.JSONDecodeError:
                continue
        if items:
            return items

    return None


def _truncate(text: str, max_words: int) -> str:
    """Truncate text to max_words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "..."
