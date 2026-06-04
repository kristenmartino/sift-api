from __future__ import annotations

import json
import logging

import anthropic

from app.config import settings
from app.db import get_pool
from services.batch_client import submit_batch
from services.cost_guard import check_budget
from services.judge import judge_lines, judge_rejects
from services.quality_gate import gate_why_it_matters
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.context_generator")

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 10

BATCH_KIND = "context"  # identifier persisted to api_batches.kind

# Rough Sonnet judge cost per line (input title+summary+line + short output),
# used only to pre-check the cost guard before the optional runtime judge.
JUDGE_COST_PER_LINE_USD = 0.003


# ---------------------------------------------------------------------------
# Prompt — single source of truth for both the live and batch paths.
#
# Rubric (sift-api#90): the why_it_matters line must surface a CONCRETE,
# VERIFIABLE stake not already in the title/summary; strictly neutral; no
# restatement; no editorializing/clichés; and return "" when there is no real
# stake (null-over-filler — an absent line renders nothing, which beats fluff).
# The importance score is independent and always provided. The deterministic
# quality_gate runs over the output as a backstop; this prompt is the primary
# semantic gate.
# ---------------------------------------------------------------------------

def _build_articles_text(batch: list[dict]) -> str:
    articles_text = ""
    for i, article in enumerate(batch, 1):
        articles_text += (
            f"\n{i}. \"{article['title']}\"\n"
            f"   Summary: {article['summary']}\n"
        )
    return articles_text


def _build_context_prompt(batch: list[dict]) -> str:
    return f"""For each article below, provide two independent things.

1. A "why it matters" line (key "c"). ONE neutral sentence, max 18 words, that \
gives the reader a CONCRETE, VERIFIABLE stake that is NOT already stated in the \
title or summary — a specific consequence, who is affected, a number, a \
precedent, or what changes next. Add a fact, not a feeling.

   Hard rules for the line:
   - Do NOT restate or paraphrase the title or summary. If the only thing you \
can say is already there, return "" (an empty string).
   - Do NOT editorialize, speculate, or hand-wave. Banned phrasings include: \
"raises serious questions", "worth watching", "a turning point", "could \
finally…", "sends a message", "remains to be seen", "a wake-up call", and \
emotional color like "the most tortured fans finally have hope".
   - Strictly neutral. Never imply whether something is good or bad.
   - Vary your sentence openings. Never start with "This matters because".
   - When in doubt, return "". An empty line is the CORRECT answer when there \
is no real, neutral, verifiable stake beyond what the summary already says — the \
card simply shows nothing. Better empty than filler.

2. An importance score from 1-5 (key "s"), independent of the line above — \
always provide it, even when the line is empty:
   1 = routine/minor (local interest, incremental update)
   2 = somewhat notable (industry-specific, modest impact)
   3 = noteworthy (broad interest, clear significance)
   4 = significant (wide impact, affects many people)
   5 = breaking/major (historic, urgent, massive consequence)

Articles:
{_build_articles_text(batch)}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, c=why-it-matters line (string; "" when there is no real stake), s=score.
[{{"i":1,"c":"Concrete verifiable stake here, or an empty string.","s":3}}, ...]

Return ONLY the JSON array, no other text."""


# ---------------------------------------------------------------------------
# Live path (used by backfill scripts + as a manual fallback). Routine ingest
# uses the Batch API path below for the 50% discount.
# ---------------------------------------------------------------------------

async def generate_context(articles: list[dict]) -> dict[str, dict]:
    """
    Batch-generate 'why it matters' one-liners and importance scores via Claude Haiku.

    Input: list of dicts with keys: source_url, title, summary
    Output: dict mapping source_url -> {"context": str | None, "score": int}

    `context` is None when the quality gate drops the line (no real stake); the
    score is still returned so callers can record it independently.
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

    kept = sum(1 for r in results.values() if r["context"])
    logger.info(
        "Generated context for %d/%d articles (%d kept after gate, %d dropped)",
        len(results), len(articles), kept, len(results) - kept,
    )
    return results


async def _generate_batch(
    client: anthropic.AsyncAnthropic,
    batch: list[dict],
) -> dict[str, dict]:
    """Send a batch of articles to Claude Haiku for context + importance generation."""
    response = await client.messages.create(
        model=MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": _build_context_prompt(batch)}],
    )
    log_usage("context_generator.batch", response, model=MODEL)

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_context(text, batch)


def _parse_context(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's context + score response, applying the quality gate.

    The line and the score are decoupled: a line dropped by the gate (or returned
    empty by the model) still yields a row carrying the importance score, with
    context=None so the caller writes NULL why_it_matters.
    """
    results: dict[str, dict] = {}

    parsed = _extract_json_array(text)
    if not parsed:
        logger.warning("Failed to parse context generation JSON")
        return results

    for item in parsed:
        # Accept short keys (new) and fall back to long keys (legacy prompt form).
        idx = item.get("i", item.get("index"))
        raw_context = item.get("c", item.get("context", ""))
        score = item.get("s", item.get("score", 3))
        if not (isinstance(idx, int) and 1 <= idx <= len(batch)):
            continue

        # Clamp score to 1-5 (always recorded, independent of the line).
        if not isinstance(score, int) or score < 1 or score > 5:
            score = 3

        article = batch[idx - 1]
        gated = gate_why_it_matters(
            raw_context, title=article.get("title", ""), summary=article.get("summary", ""),
        )
        results[article["source_url"]] = {"context": gated, "score": score}

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
# Batch API path (Phase 6) — same prompt, submitted via Message Batches for the
# 50% cost discount. Results are processed asynchronously by the poller.
# ---------------------------------------------------------------------------

async def submit_context_batch(articles: list[dict]) -> str | None:
    """Submit context generation via Message Batches API (50% cheaper).

    articles: list of {source_url, title, summary}.
    Each sub-batch of BATCH_SIZE articles becomes one request with
    custom_id = "ctx-<n>" so the result handler can map back to the articles
    table via the persisted metadata.

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
                "messages": [{"role": "user", "content": _build_context_prompt(sub)}],
            },
        })

    # Metadata maps custom_id -> list of source_urls so the handler can match
    # JSONL results back to articles. title/summary needed for gating at poll
    # time are read from the articles table (already stored by store_node).
    metadata = {
        f"ctx-{i // BATCH_SIZE}": [a["source_url"] for a in articles[i : i + BATCH_SIZE]]
        for i in range(0, len(articles), BATCH_SIZE)
    }
    return await submit_batch(BATCH_KIND, requests, metadata=metadata)


async def process_context_batch_results(batch_id: str, results: list[dict]) -> None:
    """Poller callback. Parses JSONL results, runs the quality gate, and UPDATEs
    articles with why_it_matters + importance_score.

    The line and score are decoupled: a line dropped by the gate stores NULL
    why_it_matters while still recording importance_score. title/summary for the
    gate's restatement check are read from the articles table in one query per
    sub-batch (the rows exist by now — store_node runs before the batch lands).
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
    dropped = 0
    judge_dropped = 0
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

        # One read for the whole sub-batch: title/summary feed the gate.
        meta_by_url: dict[str, tuple[str, str]] = {}
        if urls:
            try:
                meta_rows = await pool.fetch(
                    "SELECT source_url, title, summary FROM articles "
                    "WHERE source_url = ANY($1::text[])",
                    urls,
                )
                meta_by_url = {
                    r["source_url"]: (r["title"] or "", r["summary"] or "")
                    for r in meta_rows
                }
            except Exception as e:
                logger.error("context gate metadata read failed for %s: %s", custom_id, e)

        # Deterministic gate first; collect per-row results for this sub-batch.
        pending: list[dict] = []
        for entry in parsed:
            idx = entry.get("i", entry.get("index"))
            raw_context = entry.get("c", entry.get("context", ""))
            score = entry.get("s", entry.get("score", 3))
            if not (isinstance(idx, int) and 1 <= idx <= len(urls)):
                continue
            if not isinstance(score, int) or score < 1 or score > 5:
                score = 3

            url = urls[idx - 1]
            title, summary = meta_by_url.get(url, ("", ""))
            gated = gate_why_it_matters(raw_context, title=title, summary=summary)
            if gated is None:
                dropped += 1
            pending.append({
                "url": url, "line": gated, "score": score, "title": title, "summary": summary,
            })

        # Optional runtime judge over the survivors (sift-api#90, off by default).
        # Catches the paraphrase/editorial residual the cheap gate can't. One
        # judge call per sub-batch; skipped (lines kept) when the cost guard
        # blocks it, so judging never blocks storage and a judge error degrades
        # to the deterministic result.
        if settings.why_it_matters_judge_enabled:
            kept = [p for p in pending if p["line"]]
            if kept:
                budget = await check_budget(JUDGE_COST_PER_LINE_USD * len(kept))
                if budget.allowed:
                    verdicts = await judge_lines([
                        {"id": p["url"], "title": p["title"], "summary": p["summary"], "line": p["line"]}
                        for p in kept
                    ])
                    by_url = {v["id"]: v for v in verdicts}
                    for p in kept:
                        if judge_rejects(by_url.get(p["url"], {})):
                            p["line"] = None
                            judge_dropped += 1
                else:
                    logger.info("context runtime judge skipped (%s) for %s", budget.reason, custom_id)

        for p in pending:
            try:
                await pool.execute(
                    """
                    UPDATE articles
                       SET why_it_matters = $1,
                           importance_score = $2,
                           updated_at = NOW()
                     WHERE source_url = $3
                    """,
                    p["line"], p["score"], p["url"],
                )
                updated += 1
            except Exception as e:
                logger.error("UPDATE why_it_matters for %s failed: %s", p["url"], e)
                failed += 1

    logger.info(json.dumps({
        "event": "batch_context_applied",
        "batch_id": batch_id,
        "updated": updated,
        "dropped_by_gate": dropped,
        "dropped_by_judge": judge_dropped,
        "failed": failed,
    }))
