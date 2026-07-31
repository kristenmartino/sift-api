from __future__ import annotations

import functools
import json
import logging
import re

import anthropic

from app.config import settings
from app.models import RSSArticle
from services.quality_gate import gate_summary
from services.index_alignment import (
    MAX_BATCH_ATTEMPTS,
    AlignmentError,
    aligned_entries,
    with_alignment_retry,
)
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.summarizer")

BATCH_SIZE = 5
MODEL = "claude-haiku-4-5-20251001"

VALID_CATEGORIES = {"top", "technology", "business", "science", "energy", "world", "health", "politics", "sports", "entertainment"}


async def summarize_articles(
    articles: list[RSSArticle],
    *,
    client: anthropic.AsyncAnthropic | None = None,
) -> dict[str, dict]:
    """
    Summarize and classify articles in batches using Claude Haiku.
    Returns a dict mapping source_url to {"summary": str, "category": str}.

    Optional injected client so tests (and any future eval harness) can replay
    a recorded response instead of paying for a live call. Mirrors the same
    kwarg on services.story_clusterer.cluster_articles.
    """
    if not articles:
        return {}

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
    results: dict[str, dict] = {}
    misaligned_batches = 0

    # Articles whose RSS entry is a headline and nothing else cannot be
    # summarized, and asking anyway is how apologies ended up on cards
    # (#118): the model answers "Insufficient content provided to summarize."
    # and that gets stored. Skipping is also free — these were paying for a
    # model call to produce something unusable.
    summarizable = [a for a in articles if _has_summarizable_content(a)]
    skipped_empty = len(articles) - len(summarizable)
    if skipped_empty:
        logger.info(
            "Skipping %d/%d articles whose RSS entry carries no body text",
            skipped_empty, len(articles),
        )

    for i in range(0, len(summarizable), BATCH_SIZE):
        batch = summarizable[i : i + BATCH_SIZE]
        batch_index = i // BATCH_SIZE
        try:
            results.update(await _summarize_batch_with_retry(client, batch, batch_index))
        except AlignmentError as e:
            misaligned_batches += 1
            logger.error(
                "Summarization batch %d abandoned after %d misaligned attempts: %s",
                batch_index,
                MAX_BATCH_ATTEMPTS,
                e,
            )
            results.update(_raw_content_fallback(batch))
        except Exception as e:
            logger.error("Summarization failed for batch %d: %s", batch_index, e)
            results.update(_raw_content_fallback(batch))

    logger.info(
        "Summarized %d/%d articles (%d batches fell back after misalignment)",
        len(results),
        len(articles),
        misaligned_batches,
    )
    return results


async def _summarize_batch_with_retry(
    client: anthropic.AsyncAnthropic,
    batch: list[RSSArticle],
    batch_index: int,
) -> dict[str, dict]:
    """Ask for a batch, re-asking while the response cannot be proven aligned."""
    return await with_alignment_retry(
        functools.partial(_summarize_batch, client, batch),
        logger=logger,
        event="summary_batch_misaligned",
        batch_index=batch_index,
        ids=[a.source_url for a in batch],
    )


def _has_summarizable_content(article: RSSArticle) -> bool:
    """False when the RSS entry gives us nothing beyond the headline.

    `_build_prompt` falls back to the title when raw_content is empty, which
    asks the model to summarize a headline from itself — it answers with an
    apology, and that apology used to be stored (#118). Deliberately narrow:
    only genuinely empty or title-duplicate bodies are skipped, because thin
    content can still summarize fine and quality_gate.gate_summary catches
    whatever slips through.
    """
    content = re.sub(r"<[^>]+>", "", article.raw_content or "").strip()
    if not content:
        return False
    return content.casefold() != (article.title or "").strip().casefold()


def _raw_content_fallback(batch: list[RSSArticle]) -> dict[str, dict]:
    """Degraded per-article summaries: the article's own RSS content, truncated.

    Crude, but alignment-proof — each summary is built from the article it is
    keyed to, so no cross-article mix-up is possible. Preferred over writing
    nothing because `services.deduplicator` drops known source_urls before
    summarization, so an article stored with an empty summary is never
    re-summarized on a later cycle; "the next run will fix it" is not true at
    the pipeline level.
    """
    return {
        article.source_url: {
            "summary": _truncate(article.raw_content, 200),
            "category": "top",
        }
        for article in batch
        if article.raw_content
    }


async def _summarize_batch(
    client: anthropic.AsyncAnthropic,
    batch: list[RSSArticle],
) -> dict[str, dict]:
    """Send a batch of articles to Claude Haiku and parse summaries + categories."""
    prompt = _build_prompt(batch)

    response = await client.messages.create(
        model=MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    log_usage("summarizer.batch", response, model=MODEL)

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

Rules about people and legal matters — these override everything above:
- Describe only what the source article states. Do not add facts, motives, or \
history that are not in the text you were given.
- Never characterize a legal outcome beyond what the source literally says. \
"Charged" is not "guilty." "Settled" is not "found liable." An investigation is \
not a finding. An accusation is not a fact.
- Attribute contested claims to whoever made them: "prosecutors say," "the \
complaint alleges," "according to <outlet>."
- A campaign contribution is not an endorsement. A committee seat is not a \
position. A vote is not a belief.
- If the article's own framing is uncertain, keep the uncertainty. Do not \
resolve it.

{articles_text}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, s=summary, c=category.
[{{"i":1,"s":"1-2 sentence summary","c":"technology"}}, ...]

Return ONLY the JSON array, no other text."""


def _parse_summaries(text: str, batch: list[RSSArticle]) -> dict[str, dict]:
    """Parse Claude's response into a url -> {summary, category} mapping.

    All-or-nothing by design: the indices must form exactly {1..len(batch)}
    (enforced by services.index_alignment.aligned_entries) and every article
    must get a non-empty summary. Anything else raises and the caller re-asks
    rather than writing a summary that may belong to a different article.

    There is deliberately no positional line-by-line fallback. Mapping raw
    output lines to articles by position turns a single preamble line ("Here
    are the summaries:") into an off-by-one across the whole batch. Callers
    retry, then degrade to _raw_content_fallback, which cannot misalign.
    """
    parsed = _extract_json_array(text)
    if parsed is None:
        raise AlignmentError("response was not a parseable JSON array")

    results: dict[str, dict] = {}
    for idx, entry in aligned_entries(parsed, len(batch)).items():
        # Accept short keys (new) and fall back to long keys (legacy prompt form).
        summary = entry.get("s", entry.get("summary", ""))
        category = entry.get("c", entry.get("category", "top"))
        # An unrecognized category label says nothing about alignment; coerce
        # it as before rather than throwing the batch away.
        if category not in VALID_CATEGORIES:
            category = "top"
        # An article with no summary of its own is a missing entry wearing a
        # valid index — same evidence of a shift, same all-or-nothing answer.
        # Checked on the RAW text, BEFORE the refusal gate: a refusal is a
        # (bad) answer for this article, not evidence that the batch shifted.
        if not isinstance(summary, str) or not summary.strip():
            raise AlignmentError(f"empty summary at index {idx}")

        # Drop the model's own apologies for articles it could not summarize
        # (#118). May legitimately empty the summary; store_node writes "" and
        # the feed skips it, which beats showing the apology.
        summary = gate_summary(summary)

        results[batch[idx - 1].source_url] = {"summary": summary, "category": category}

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
