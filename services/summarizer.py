from __future__ import annotations

import functools
import json
import logging
import re

import anthropic

from app.config import settings
from app.models import RSSArticle
from services.cost_estimates import estimate_cost
from services.cost_guard import check_budget
from services.model_registry import resolve
from services.quality_gate import gate_summary
from services.index_alignment import (
    MAX_BATCH_ATTEMPTS,
    AlignmentError,
    aligned_entries,
    with_alignment_retry,
)
from services.usage_tracker import log_output_stop, log_usage

logger = logging.getLogger("sift-api.summarizer")

BATCH_SIZE = 5
OPERATION = "summarizer.batch"

# Output ceiling for one batch — DERIVED, not a constant, because the two
# things it has to cover both move.
#
# It was a flat 700, which was comfortable when written and is not any more.
# #240 stopped discarding `content:encoded`, so the model now reads far more
# of each article and writes marginally fuller summaries: measured peak output
# went 481 -> 567 of 700 between 2026-08-11 and 08-17 (81% of the ceiling)
# while mean output moved only 332 -> 359. Summaries are length-bounded by the
# prompt ("1-2 concise sentences"), not by input size, so this drifts rather
# than runs away — but it drifts toward a cliff.
#
# The cliff is sharp and silent: a response cut off at the cap is truncated
# JSON, which fails `index_alignment`, which re-asks and then degrades the
# WHOLE batch to `_raw_content_fallback` — five articles served truncated RSS
# text while the run reports success. This repo has now hit that twice, both
# times from a fixed ceiling: `story_synthesizer`'s max_tokens=1024 was
# breaking exactly its biggest clusters, and gpt-5-nano produced 30/30 empty
# batches at 700 by spending the whole budget reasoning.
#
# Raising it is close to free — `max_tokens` bills on tokens *used*, not on
# the ceiling — so the asymmetry is one-sided: an unused ceiling costs
# nothing, a binding one costs five articles.
#
# Per-article budget is ~2x the measured peak: the worst observed batch was
# 567 for five articles, ~105/article after scaffolding.
OUTPUT_TOKENS_PER_ARTICLE = 240
OUTPUT_TOKENS_SCAFFOLDING = 120  # the JSON array, keys and index fields
MAX_OUTPUT_TOKENS = BATCH_SIZE * OUTPUT_TOKENS_PER_ARTICLE + OUTPUT_TOKENS_SCAFFOLDING

VALID_CATEGORIES = {"top", "technology", "business", "science", "energy", "world", "health", "politics", "sports", "entertainment"}

# Sink for articles whose category the model failed to provide (unrecognized
# label, or a whole batch degraded to _raw_content_fallback). Maps to no feed
# tab: the row is stored and searchable but never ranked. The old behavior —
# coercing to "top" — funneled exactly the least-classifiable content into the
# most visible tab. Misfiling by policy is worse than not filing.
FALLBACK_CATEGORY = "general"


def _batch_count(items: list) -> int:
    return -(-len(items) // BATCH_SIZE)


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

    # Daily AI cost ceiling. Checked once for the whole set rather than per
    # batch: the guard exists to stop a runaway day, and a ledger read per batch
    # would add ~8 round trips a run for no extra protection.
    #
    # DEGRADES TO `_raw_content_fallback`, NOT TO `{}`, AND THAT IS LOAD-BEARING.
    # `store_node` iterates `new_articles`, not `summaries`, so an article
    # missing from this dict is still stored — with no summary — and
    # `services.deduplicator` drops its source_url on every later cycle, so it
    # is never re-summarized. Returning nothing would permanently blank a whole
    # budget-stopped day. Truncated RSS text is worse than a real summary and
    # far better than that; same reasoning as `_raw_content_fallback`'s own
    # docstring.
    budget = await check_budget(estimate_cost(OPERATION, _batch_count(summarizable)))
    if not budget.allowed:
        logger.warning(
            "Summarization skipped for %d articles (cost guard: %s); storing "
            "truncated RSS content so the rows are not left permanently blank.",
            len(summarizable), budget.reason,
        )
        return _raw_content_fallback(summarizable)

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
            "category": FALLBACK_CATEGORY,
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
    spec = resolve(OPERATION)

    response = await client.messages.create(
        model=spec.model,
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    log_usage(OPERATION, response, model=spec.model)

    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    # Record how the call ended, split on whether it aligned. Measured over
    # 212 calls on 2026-08-11: 1 misaligned (0.5%), 0 ended in max_tokens,
    # peak output 481 of 700 — so truncation is not why batches misalign, and
    # this ceiling is not close to binding at BATCH_SIZE = 5. The split is
    # kept because it is the only stored signal for whether a model returns
    # parseable indexed JSON at all. See migrations/021.
    try:
        parsed = _parse_summaries(text, batch)
    except AlignmentError as e:
        log_output_stop(
            "summarizer.batch", response, aligned=False, batch_size=len(batch),
        )
        # Carry the response context onto the error so the retry log names the
        # stop reason too — the table gives the rate, the log gives the case.
        e.stop_reason = getattr(response, "stop_reason", None)
        e.output_tokens = int(
            getattr(getattr(response, "usage", None), "output_tokens", 0) or 0
        )
        e.max_output_tokens = MAX_OUTPUT_TOKENS
        raise

    log_output_stop(
        "summarizer.batch", response, aligned=True, batch_size=len(batch),
    )
    return parsed


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
- "top" — reserved for stories of major, broad public significance: breaking news of national or international consequence, or stories that genuinely cut across several of the categories below. A story is NOT "top" merely because it is dramatic, violent, or shocking. Single-incident crime, accident, or death stories (a killing, a shooting, an assault charge, a fatal crash, a trial) are never "top" — classify them into the closest topical category instead
- "technology" — tech industry, software, hardware, AI, cybersecurity, social media
- "business" — Wall Street, stock market, earnings reports, M&A, IPOs, venture capital, interest rates, Federal Reserve, banking, employment data, GDP, inflation, corporate strategy, trade policy. NOT consumer product launches, pop culture brands, or retail sales events
- "science" — research, discoveries, space, physics, biology, climate science
- "energy" — power grid, renewables, oil & gas, EVs, energy policy, utilities
- "world" — international affairs, geopolitics, diplomacy, foreign policy
- "health" — medicine, public health, pharma, healthcare policy, disease
- "politics" — elections, legislation, political parties, Congress, campaigns, government policy
- "sports" — professional sports, college sports, Olympics, player trades, game results
- "entertainment" — movies, TV, music, celebrities, streaming, awards, pop culture, consumer product launches, brand collaborations, viral consumer trends

Most articles must go into a specific topic category — when unsure, prefer the specific category over "top".

Routing rule for crime, accident, and death stories: these are "top" only when \
the event itself has national or international consequence (a mass-casualty \
attack, the assassination of a public figure, a disaster prompting a national \
response). Otherwise classify by setting or participants: sports figures → \
"sports"; entertainers or media personalities → "entertainment"; corporate or \
financial wrongdoing → "business"; policing, courts, or justice-system policy → \
"politics"; incidents outside the U.S. → "world"; deaths with public-health \
relevance (overdoses, outbreaks) → "health". If none fit, pick the closest \
category anyway — never default to "top".

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
        category = entry.get("c", entry.get("category", FALLBACK_CATEGORY))
        # An unrecognized category label says nothing about alignment; coerce
        # it rather than throwing the batch away. Normalize first — "Top" and
        # " technology " are answers, not noise — then sink whatever remains.
        category = str(category).strip().lower()
        if category not in VALID_CATEGORIES:
            logger.info(
                "Unrecognized category %r at index %d coerced to %r",
                entry.get("c", entry.get("category")), idx, FALLBACK_CATEGORY,
            )
            category = FALLBACK_CATEGORY
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
