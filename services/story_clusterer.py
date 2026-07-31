from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.story_clusterer")

MODEL = "claude-haiku-4-5-20251001"


def build_prompt(articles: list[dict]) -> str:
    """Build the clustering prompt for `articles`.

    Extracted from cluster_articles so the eval harness can hash it. Each
    recorded response fixture stores sha256(build_prompt(batch)); the replay
    test recomputes it and fails loudly on a mismatch, so a changed prompt can
    never be silently scored against a stale recorded response.

    Output is byte-identical to the previous inline construction — do not
    reformat casually, it would invalidate every committed fixture.
    """
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

    return f"""You are grouping news articles that cover THE SAME specific event.

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


async def cluster_articles(
    articles: list[dict],
    *,
    client: anthropic.AsyncAnthropic | None = None,
    model: str = MODEL,
) -> list[dict]:
    """
    LLM-as-judge clustering: group articles covering the same event.

    Input: list of dicts with keys: source_url, title, summary, source_name, entities
    Output: list of cluster dicts: [{group_id, article_indices, event}]
           article_indices are 1-based matching the prompt numbering.

    Optional reusable client so an eval run shares one connection — and so the
    eval harness can replay a recorded response instead of paying for a live
    call. Mirrors the same kwarg on services.judge.judge_lines.
    """
    if len(articles) < 2:
        return []

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = build_prompt(articles)

    try:
        response = await client.messages.create(
            model=model,
            # Scale the ceiling with the input. A fixed 1024 silently truncated
            # the JSON array on large windows: the cut-off text failed
            # _extract_json_array, _parse_clusters returned [], and the category
            # produced ZERO stories with nothing logged. ~40 tokens covers one
            # group object; 128 covers the array scaffolding.
            max_tokens=_max_tokens_for(len(articles)),
            messages=[{"role": "user", "content": prompt}],
        )
        log_usage("story_clusterer.cluster", response, model=model)

        text = "".join(b.text for b in response.content if b.type == "text")
        clusters = _parse_clusters(text, len(articles))
        stop_reason = getattr(response, "stop_reason", None)

        # Structured so "no overlapping coverage" is distinguishable from
        # "response truncated" and "parse failed" — previously all three looked
        # identical in the logs. stop_reason == "max_tokens" is the signal that
        # was invisible.
        logger.info(json.dumps({
            "event": "cluster_stats",
            "n_articles": len(articles),
            "n_groups": len(clusters),
            "stop_reason": stop_reason,
            "parsed": bool(text.strip()) and _extract_json_array(text) is not None,
            "max_tokens": _max_tokens_for(len(articles)),
        }))
        if stop_reason == "max_tokens":
            logger.warning(
                "Clustering response hit the output ceiling (%d articles) — "
                "groups were likely lost to truncation",
                len(articles),
            )
        return clusters
    except Exception as e:
        logger.error("Clustering failed: %s", e)
        return []


def _max_tokens_for(article_count: int) -> int:
    """Output ceiling for a clustering call, scaled to the input size."""
    return min(2048, 128 + 40 * article_count)


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
