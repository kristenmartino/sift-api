from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings

logger = logging.getLogger("sift-api.story_clusterer")

MODEL = "claude-haiku-4-5-20251001"


async def cluster_articles(articles: list[dict]) -> list[dict]:
    """
    LLM-as-judge clustering: group articles covering the same event.

    Input: list of dicts with keys: source_url, title, summary, source_name, entities
    Output: list of cluster dicts: [{group_id, article_indices, event}]
           article_indices are 1-based matching the prompt numbering.
    """
    if len(articles) < 2:
        return []

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    articles_text = ""
    for i, article in enumerate(articles, 1):
        entities = article.get("entities", {})
        entity_str = ""
        if entities:
            parts = []
            for key in ("people", "organizations", "locations"):
                vals = entities.get(key, [])
                if vals:
                    parts.append(f"{key}: {', '.join(vals)}")
            if parts:
                entity_str = f" — {'; '.join(parts)}"
        articles_text += (
            f"{i}. [{article['source_name']}] \"{article['title']}\"{entity_str}\n"
            f"   {article['summary']}\n\n"
        )

    prompt = f"""You are grouping news articles that cover THE SAME specific event.

Important: "same event" means the same specific occurrence — not just the same broad topic.
For example, "EU votes on AI Act" and "US issues AI executive order" are the same TOPIC (AI regulation) but DIFFERENT events. Do NOT group them.

Articles:
{articles_text}

Group articles that cover the same specific event. Each group must have at least 2 articles.
Articles not in any group should be omitted from the output.

Return a JSON array of groups:
[{{"group_id": 1, "article_indices": [1, 3], "event": "brief description of the shared event"}}]

If no articles cover the same event, return an empty array: []

Return ONLY the JSON array, no other text."""

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = "".join(b.text for b in response.content if b.type == "text")
        clusters = _parse_clusters(text, len(articles))
        logger.info("Clustered %d articles into %d groups", len(articles), len(clusters))
        return clusters
    except Exception as e:
        logger.error("Clustering failed: %s", e)
        return []


def _parse_clusters(text: str, article_count: int) -> list[dict]:
    """Parse Claude's clustering response, validating indices."""
    parsed = _extract_json_array(text)
    if not parsed:
        return []

    valid_clusters = []
    for cluster in parsed:
        indices = cluster.get("article_indices", [])
        event = cluster.get("event", "")
        group_id = cluster.get("group_id", len(valid_clusters) + 1)

        # Validate: at least 2 articles, all indices in range
        if (
            len(indices) >= 2
            and all(isinstance(i, int) and 1 <= i <= article_count for i in indices)
        ):
            valid_clusters.append({
                "group_id": group_id,
                "article_indices": indices,
                "event": event,
            })

    return valid_clusters


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
