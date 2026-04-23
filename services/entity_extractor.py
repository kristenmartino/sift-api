from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from app.db import get_pool
from services.batch_client import submit_batch
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.entity_extractor")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 15  # More articles per call since extraction is lighter than summarization

BATCH_KIND = "entity"  # identifier persisted to api_batches.kind


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

    # Short JSON keys to reduce output tokens (output is 5x more expensive than input).
    # i=index, p=people, o=organizations, l=locations, e=event_description
    prompt = f"""Extract named entities from each article. Use short JSON keys.

Keys:
- i: 1-based article index
- p: people (list of names)
- o: organizations (list of companies, governments, agencies)
- l: locations (list of places, countries, regions)
- e: brief event phrase (5-10 words)

{articles_text}

Return a JSON array, one object per article, in order:
[{{"i":1,"p":["Name"],"o":["Org"],"l":["Place"],"e":"brief event"}}, ...]

Return ONLY the JSON array, no other text."""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1400,
        messages=[{"role": "user", "content": prompt}],
    )
    log_usage("entity_extractor.batch", response, model=MODEL)

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_entities(text, batch)


def _parse_entities(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's entity extraction response."""
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if parsed:
        for item in parsed:
            # Accept short keys (new) and fall back to long keys (legacy prompt form).
            idx = item.get("i", item.get("index"))
            if isinstance(idx, int) and 1 <= idx <= len(batch):
                results[batch[idx - 1]["source_url"]] = {
                    "people": item.get("p", item.get("people", [])),
                    "organizations": item.get("o", item.get("organizations", [])),
                    "locations": item.get("l", item.get("locations", [])),
                    "event_description": item.get("e", item.get("event_description", "")),
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


# ---------------------------------------------------------------------------
# Batch API path (Phase 6b) — same prompt, submitted via Message Batches for
# 50% cost discount. Results are processed asynchronously by the poller and
# written to articles.entities. The story threading workflow then reads
# entities from the DB (articles without entities yet get excluded that
# cycle and picked up on the next).
# ---------------------------------------------------------------------------

def _build_batch_prompt(batch: list[dict]) -> str:
    articles_text = ""
    for i, article in enumerate(batch, 1):
        articles_text += (
            f"\n{i}. [{article['source_name']}] \"{article['title']}\"\n"
            f"   Summary: {article['summary']}\n"
        )
    return f"""Extract named entities from each article. Use short JSON keys.

Keys:
- i: 1-based article index
- p: people (list of names)
- o: organizations (list of companies, governments, agencies)
- l: locations (list of places, countries, regions)
- e: brief event phrase (5-10 words)

{articles_text}

Return a JSON array, one object per article, in order:
[{{"i":1,"p":["Name"],"o":["Org"],"l":["Place"],"e":"brief event"}}, ...]

Return ONLY the JSON array, no other text."""


async def submit_entity_batch(articles: list[dict]) -> str | None:
    """Submit entity extraction via Message Batches API (50% cheaper).

    articles: list of {source_url, title, summary, source_name}.
    Each sub-batch of BATCH_SIZE articles becomes one request with
    custom_id = "ent-<N>"; the URL manifest is persisted in
    api_batches.metadata so the result handler can map results back.

    Returns the batch_id (or None if submission failed / no input).
    """
    if not articles:
        return None

    requests: list[dict] = []
    for i in range(0, len(articles), BATCH_SIZE):
        sub = articles[i : i + BATCH_SIZE]
        custom_id = f"ent-{i // BATCH_SIZE}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL,
                "max_tokens": 1400,
                "messages": [{"role": "user", "content": _build_batch_prompt(sub)}],
            },
        })

    metadata = {
        f"ent-{i // BATCH_SIZE}": [a["source_url"] for a in articles[i : i + BATCH_SIZE]]
        for i in range(0, len(articles), BATCH_SIZE)
    }
    return await submit_batch(BATCH_KIND, requests, metadata=metadata)


async def process_entity_batch_results(batch_id: str, results: list[dict]) -> None:
    """Poller callback. Parses JSONL results and UPDATEs articles.entities."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT metadata FROM api_batches WHERE batch_id = $1", batch_id,
    )
    if row is None:
        logger.error("process_entity_batch_results: batch %s not in api_batches", batch_id)
        return

    raw_meta = row["metadata"]
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except json.JSONDecodeError:
            raw_meta = {}
    custom_id_to_urls: dict[str, list[str]] = raw_meta or {}

    updated = 0
    failed = 0
    for item in results:
        custom_id = item.get("custom_id", "")
        urls = custom_id_to_urls.get(custom_id, [])
        result = item.get("result", {})
        if result.get("type") != "succeeded":
            failed += 1
            continue

        message = result.get("message", {})
        content_blocks = message.get("content", []) or []
        text = "".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "text"
        )
        parsed = _extract_json_array(text)
        if not parsed:
            failed += 1
            continue

        for entry in parsed:
            idx = entry.get("i", entry.get("index"))
            if not (isinstance(idx, int) and 1 <= idx <= len(urls)):
                continue
            entities = {
                "people": entry.get("p", entry.get("people", [])),
                "organizations": entry.get("o", entry.get("organizations", [])),
                "locations": entry.get("l", entry.get("locations", [])),
                "event_description": entry.get("e", entry.get("event_description", "")),
            }
            url = urls[idx - 1]
            try:
                await pool.execute(
                    """
                    UPDATE articles
                       SET entities = $1::jsonb,
                           updated_at = NOW()
                     WHERE source_url = $2
                    """,
                    json.dumps(entities), url,
                )
                updated += 1
            except Exception as e:
                logger.error("UPDATE entities for %s failed: %s", url, e)
                failed += 1

    logger.info(json.dumps({
        "event": "batch_entity_applied",
        "batch_id": batch_id,
        "updated": updated,
        "failed": failed,
    }))
