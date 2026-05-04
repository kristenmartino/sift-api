"""Background-primer generator — Phase 1A of the civic-literacy MVP.

For each article, generates a "What you should know first" panel:
- background: one short paragraph of context the reader needs before the lede
- terms: 3-5 key terms surfaced from the article body, each with a brief
  plain-language definition

The output lives at articles.context_primer (JSONB) and is rendered by the
sift/components/primer/BackgroundPrimer.tsx component.

Runs via Anthropic's Message Batches API for the 50% discount, mirroring the
context_generator + entity_extractor patterns. Pipeline submits batches and
returns immediately; the batch_poller invokes process_primer_batch_results
when batches complete (typically within minutes).

Voice: the patient teacher who never makes you feel dumb. Authoritative,
never preachy, never partisan, never editorializing. See lib/copy.ts in
the sift frontend for the full voice doc.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import anthropic

from app.config import settings
from app.db import get_pool
from services.batch_client import submit_batch
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.primer_generator")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 5  # primer is more tokens out per article than one-liner context

BATCH_KIND = "primer"  # identifier persisted to api_batches.kind

# Voice/format guard. Keep prompt-engineering tight: the LLM gets one job per
# article (write a primer + 3-5 terms) and outputs strict JSON. The prompt is
# the same in both the live and batch paths.
_PROMPT_HEADER = """You are writing the "What you should know first" panel for a civic-literacy news app. \
For each article below, write a brief teaching primer for a smart American adult who may have missed key context.

Two things per article:

1. background — ONE short paragraph (max 60 words) of context the reader \
needs before reading the article. Cover what's at stake, who the players are, or what came before — whichever \
the reader most likely doesn't already know. Conversational tone. Active voice. Contractions OK. \
NEVER editorialize, NEVER take a political side, NEVER tell the reader what to think.

2. terms — 3 to 5 key terms from the article that benefit from a one-sentence plain-language definition. \
Pick terms a non-expert would stumble on (filibuster, cloture, basis points, antitrust review, FOMC, \
attainment standards, EBITDA, etc.) — NOT proper nouns or common words. Each term gets a max-25-word \
definition that an 8th-grader could understand.

Critical rules:
- Never use the word "context" in the primer itself.
- Never start with "This article is about" or similar meta-language.
- Never recommend a position or imply one is correct.
- If the article is short or self-contained and needs no context, return background as an empty string \
and terms as an empty array. The UI hides empty primers.

Articles:
{articles_text}

Return a JSON array with one object per article, in the same order. Use short keys to save tokens:
i = index (1-based)
b = background paragraph (string, may be empty)
t = terms array (each term: {{"term": "...", "def": "..."}})

[{{"i":1,"b":"Background paragraph here.","t":[{{"term":"filibuster","def":"A Senate procedure that requires 60 votes to end debate on most legislation."}}]}}, ...]

Return ONLY the JSON array, no other text."""


def _build_articles_text(batch: list[dict]) -> str:
    text = ""
    for i, article in enumerate(batch, 1):
        text += (
            f"\n{i}. \"{article['title']}\"\n"
            f"   Source: {article.get('source_name', 'unknown')}\n"
            f"   Summary: {article['summary']}\n"
        )
    return text


def _build_prompt(batch: list[dict]) -> str:
    return _PROMPT_HEADER.format(articles_text=_build_articles_text(batch))


# ---------------------------------------------------------------------------
# Live path (used for backfill and as a manual fallback)
# ---------------------------------------------------------------------------

async def generate_primers(articles: list[dict]) -> dict[str, dict]:
    """Generate primers for a list of articles via the live Messages API.

    Input: list of dicts with keys: source_url, title, summary, source_name (optional)
    Output: dict mapping source_url -> { background, terms, generated_at }

    For routine ingest, prefer submit_primer_batch (50% cheaper). This live path
    exists for backfill scripts and one-off jobs where async batch latency is
    unacceptable.
    """
    if not articles:
        return {}

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
    results: dict[str, dict] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            batch_results = await _generate_batch_live(client, batch)
            results.update(batch_results)
        except Exception as e:
            logger.error("Primer generation failed for batch %d: %s", i // BATCH_SIZE, e)

    logger.info("Generated primers for %d/%d articles", len(results), len(articles))
    return results


async def _generate_batch_live(
    client: anthropic.AsyncAnthropic,
    batch: list[dict],
) -> dict[str, dict]:
    response = await client.messages.create(
        model=MODEL,
        max_tokens=1500,  # ~300 tokens per article * 5 articles + headroom
        messages=[{"role": "user", "content": _build_prompt(batch)}],
    )
    log_usage("primer_generator.batch", response, model=MODEL)

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_primers(text, batch)


def _parse_primers(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's primer JSON response into the canonical persisted shape."""
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if not parsed:
        logger.warning("Failed to parse primer generation JSON")
        return results

    now_iso = datetime.now(timezone.utc).isoformat()
    for item in parsed:
        idx = item.get("i", item.get("index"))
        background = item.get("b", item.get("background", "")) or ""
        terms_raw = item.get("t", item.get("terms", [])) or []

        if not (isinstance(idx, int) and 1 <= idx <= len(batch)):
            continue

        # Normalize terms to a stable shape. Tolerate `def`/`definition` and
        # drop any malformed entries silently.
        terms: list[dict] = []
        if isinstance(terms_raw, list):
            for t in terms_raw:
                if not isinstance(t, dict):
                    continue
                term = (t.get("term") or "").strip()
                definition = (t.get("def") or t.get("definition") or "").strip()
                if term and definition:
                    terms.append({"term": term, "definition": definition})

        # Skip articles that came back fully empty — UI handles NULL too, no
        # need to write an empty record.
        if not background and not terms:
            continue

        results[batch[idx - 1]["source_url"]] = {
            "background": background.strip(),
            "terms": terms,
            "generated_at": now_iso,
        }

    return results


def _extract_json_array(text: str) -> list[dict] | None:
    """Extract a JSON array from LLM output, tolerating leading/trailing prose."""
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
# Batch API path — same prompt, submitted via Message Batches for 50% discount.
# ---------------------------------------------------------------------------

async def submit_primer_batch(articles: list[dict]) -> str | None:
    """Submit primer generation via Message Batches API.

    articles: list of {source_url, title, summary, source_name (optional)}.
    Each sub-batch of BATCH_SIZE articles becomes one request with
    custom_id = "primer-<index>" so the result handler can map back.

    Returns the batch_id (or None if submission failed / no input).
    """
    if not articles:
        return None

    requests: list[dict] = []
    for i in range(0, len(articles), BATCH_SIZE):
        sub = articles[i : i + BATCH_SIZE]
        custom_id = f"primer-{i // BATCH_SIZE}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL,
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": _build_prompt(sub)}],
            },
        })

    metadata = {
        f"primer-{i // BATCH_SIZE}": [a["source_url"] for a in articles[i : i + BATCH_SIZE]]
        for i in range(0, len(articles), BATCH_SIZE)
    }
    return await submit_batch(BATCH_KIND, requests, metadata=metadata)


async def process_primer_batch_results(batch_id: str, results: list[dict]) -> None:
    """Poller callback. Parses JSONL results and UPDATEs articles.context_primer."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT metadata FROM api_batches WHERE batch_id = $1", batch_id,
    )
    if row is None:
        logger.error("process_primer_batch_results: batch %s not in api_batches", batch_id)
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

        now_iso = datetime.now(timezone.utc).isoformat()
        for entry in parsed:
            idx = entry.get("i", entry.get("index"))
            background = entry.get("b", entry.get("background", "")) or ""
            terms_raw = entry.get("t", entry.get("terms", [])) or []

            if not (isinstance(idx, int) and 1 <= idx <= len(urls)):
                continue

            terms: list[dict] = []
            if isinstance(terms_raw, list):
                for t in terms_raw:
                    if not isinstance(t, dict):
                        continue
                    term = (t.get("term") or "").strip()
                    definition = (t.get("def") or t.get("definition") or "").strip()
                    if term and definition:
                        terms.append({"term": term, "definition": definition})

            if not background and not terms:
                continue  # UI tolerates NULL, no need to write empty record

            url = urls[idx - 1]
            primer_payload = {
                "background": background.strip(),
                "terms": terms,
                "generated_at": now_iso,
            }
            try:
                await pool.execute(
                    """
                    UPDATE articles
                       SET context_primer = $1::jsonb,
                           updated_at = NOW()
                     WHERE source_url = $2
                    """,
                    json.dumps(primer_payload), url,
                )
                updated += 1
            except Exception as e:
                logger.error("UPDATE context_primer for %s failed: %s", url, e)
                failed += 1

    logger.info(json.dumps({
        "event": "batch_primer_applied",
        "batch_id": batch_id,
        "updated": updated,
        "failed": failed,
    }))
