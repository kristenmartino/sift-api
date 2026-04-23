from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from app.db import get_pool
from services.batch_client import submit_batch
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.context_generator")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 10

BATCH_KIND = "context"  # identifier persisted to api_batches.kind


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

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, c=context one-liner, s=score.
[{{"i":1,"c":"Your one-liner here.","s":3}}, ...]

Return ONLY the JSON array, no other text."""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    log_usage("context_generator.batch", response, model=MODEL)

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_context(text, batch)


def _parse_context(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's context + score response."""
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if parsed:
        for item in parsed:
            # Accept short keys (new) and fall back to long keys (legacy prompt form).
            idx = item.get("i", item.get("index"))
            context = item.get("c", item.get("context", ""))
            score = item.get("s", item.get("score", 3))
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


# ---------------------------------------------------------------------------
# Batch API path (Phase 6) — same prompt, submitted via Message Batches for
# 50% cost discount. Results are processed asynchronously by the poller.
# ---------------------------------------------------------------------------

def _build_batch_prompt(batch: list[dict]) -> str:
    articles_text = ""
    for i, article in enumerate(batch, 1):
        articles_text += (
            f"\n{i}. \"{article['title']}\"\n"
            f"   Summary: {article['summary']}\n"
        )
    return f"""For each article below, provide two things:
1. ONE sentence (max 18 words) explaining why a general reader should care. Be direct and conversational — like a smart friend giving context. Vary your sentence openings. Never start with "This matters because". Focus on real-world impact.
2. An importance score from 1-5:
   1 = routine/minor (local interest, incremental update)
   2 = somewhat notable (industry-specific, modest impact)
   3 = noteworthy (broad interest, clear significance)
   4 = significant (wide impact, affects many people)
   5 = breaking/major (historic, urgent, massive consequence)

Articles:
{articles_text}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, c=context one-liner, s=score.
[{{"i":1,"c":"Your one-liner here.","s":3}}, ...]

Return ONLY the JSON array, no other text."""


async def submit_context_batch(articles: list[dict]) -> str | None:
    """Submit context generation via Message Batches API (50% cheaper).

    articles: list of {source_url, title, summary}.
    Each sub-batch of BATCH_SIZE articles becomes one request with
    custom_id = "ctx:<source_url_hash>" so the result handler can map
    back to the articles table.

    Returns the batch_id (or None if submission failed / no input).
    """
    if not articles:
        return None

    requests: list[dict] = []
    for i in range(0, len(articles), BATCH_SIZE):
        sub = articles[i : i + BATCH_SIZE]
        custom_id = f"ctx-{i // BATCH_SIZE}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL,
                "max_tokens": 700,
                "messages": [{"role": "user", "content": _build_batch_prompt(sub)}],
            },
        })

    # Metadata maps custom_id -> list of source_urls so the handler can
    # match JSONL results back to articles without needing Anthropic to
    # echo arbitrary data.
    metadata = {
        f"ctx-{i // BATCH_SIZE}": [a["source_url"] for a in articles[i : i + BATCH_SIZE]]
        for i in range(0, len(articles), BATCH_SIZE)
    }
    return await submit_batch(BATCH_KIND, requests, metadata=metadata)


async def process_context_batch_results(batch_id: str, results: list[dict]) -> None:
    """Poller callback. Parses JSONL results and UPDATEs articles with
    why_it_matters + importance_score.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT metadata FROM api_batches WHERE batch_id = $1", batch_id,
    )
    if row is None:
        logger.error("process_context_batch_results: batch %s not in api_batches", batch_id)
        return

    # asyncpg returns JSONB as dict already in recent versions, but may return
    # str depending on codec config. Normalize.
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
            context = entry.get("c", entry.get("context", ""))
            score = entry.get("s", entry.get("score", 3))
            if not (isinstance(idx, int) and 1 <= idx <= len(urls) and context):
                continue
            if not isinstance(score, int) or score < 1 or score > 5:
                score = 3
            url = urls[idx - 1]
            try:
                await pool.execute(
                    """
                    UPDATE articles
                       SET why_it_matters = $1,
                           importance_score = $2,
                           updated_at = NOW()
                     WHERE source_url = $3
                    """,
                    context, score, url,
                )
                updated += 1
            except Exception as e:
                logger.error("UPDATE why_it_matters for %s failed: %s", url, e)
                failed += 1

    logger.info(json.dumps({
        "event": "batch_context_applied",
        "batch_id": batch_id,
        "updated": updated,
        "failed": failed,
    }))
