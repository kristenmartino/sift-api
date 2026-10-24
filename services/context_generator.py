from __future__ import annotations

import functools
import json
import logging

import anthropic

from app.config import settings
from app.db import get_pool
from services.batch_client import submit_batch
from services.cost_estimates import estimate_cost
from services.cost_guard import check_budget
from services.model_registry import resolve
from services.index_alignment import (
    MAX_BATCH_ATTEMPTS,
    AlignmentError,
    aligned_entries,
    log_misaligned_sub_batch,
    with_alignment_retry,
)
from services.copy_rules import COPY_RULES
from services.usage_tracker import log_batch_usage, log_usage

logger = logging.getLogger("sift-api.context_generator")

OPERATION = "context_generator.batch"


def _model() -> str:
    """Resolved per call, never cached at import — an override must not need a
    restart to take effect, and a module constant would lie in tests."""
    return resolve(OPERATION).model


BATCH_SIZE = 10


def _batch_count(articles: list) -> int:
    return -(-len(articles) // BATCH_SIZE)


BATCH_KIND = "context"  # identifier persisted to api_batches.kind

# Article tone (migrations/020): the third output of the same call. Enum, not
# a score — Haiku is consistent on set membership and the D48 dampener only
# ever asks "grim or not". Anything unexpected clamps to "neutral", the
# no-penalty value, mirroring the score clamp below.
VALID_TONES = {"grim", "neutral", "light"}

# Article genre (migrations/025) — ranking v2 stage 6. is_opinion and
# is_roundup catch what outlets DECLARE (URL paths, show titles); this
# catches what they don't: magazine-style features and soft/curiosity
# pieces that read as news and can score importance 3. Deliberately NOT a
# spectacle detector — crime spectacle is already correctly scored
# importance 1-2, and the low-importance weight in sift/lib/db.ts handles
# it. Anything unexpected clamps to "news", the no-penalty value.
VALID_GENRES = {"news", "feature", "soft"}


def _clamp_tone(raw: object) -> str:
    if isinstance(raw, str) and raw.strip().lower() in VALID_TONES:
        return raw.strip().lower()
    return "neutral"


def _clamp_genre(raw: object) -> str:
    if isinstance(raw, str) and raw.strip().lower() in VALID_GENRES:
        return raw.strip().lower()
    return "news"


# Importance measures scope-of-consequence, not drama. The pre-2026-08-11
# anchors let "wide impact, affects many people" pattern-match onto emotional
# weight, so single-victim tabloid crime scored 4 and outranked elections —
# and, being exempt from the D48 grim dampener at 4+, stacked the top of the
# feed. Shared verbatim with scripts/rescore_importance.py so the re-score
# and the live prompt cannot drift.
IMPORTANCE_RUBRIC = """\
   Importance measures the SCOPE of the event's consequences — how many \
people's lives, money, safety, or rights are materially affected. It is NOT \
a measure of how dramatic, shocking, or tragic the event is: emotional \
weight is not impact, and attention is not impact.
   1 = routine/minor (local interest, incremental update)
   2 = somewhat notable (industry-specific or community-level consequence)
   3 = noteworthy (broad interest, clear significance beyond those involved)
   4 = significant (consequences materially reach many people: mass \
evacuations, major legislation or rulings, market-moving events, \
large-scale disasters)
   5 = breaking/major (historic, urgent, massive consequence)
   A crime, accident, or death with one or a few victims is a 1 or a 2 no \
matter how disturbing the details, unless the article itself states \
consequences beyond those involved (a new law or policy, a mass recall, \
charges against a major public figure). A house fire that kills a family \
is a 2; a wildfire forcing mass evacuations is a 4."""


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
    return f"""{COPY_RULES}

For each article below, classify importance, tone and genre.

1. The optional "why it matters" field (key "c") must be an empty string.
This stage receives only a headline and summary: it has no additional evidence
for a verifiable stake beyond those fields. Do not restate the summary or
invent a population, consequence, prediction, or causal link. Automatic notes
remain empty until a separate evidence source is available.

2. An importance score from 1-5 (key "s"), independent of the line above — \
always provide it, even when the line is empty:
{IMPORTANCE_RUBRIC}

3. A tone tag (key "t") — independent of both above, always provide it. \
Exactly one of:
   "grim" = the event itself is death, killing, violent crime, serious \
injury, a fatal disaster or accident, abuse, or war casualties
   "light" = feel-good, humor, entertainment, culture, scientific wonder, \
sports achievement, positive milestone
   "neutral" = everything else — including serious-but-not-deadly news \
(economic trouble, political conflict, lawsuits, layoffs, fraud, policy fights)
   Judge the event, not the writing style: a dry report of a murder is \
"grim"; an alarmed report about interest rates is "neutral". When unsure \
between "grim" and "neutral", choose "neutral".

4. A genre tag (key "g") — independent of the three above, always provide \
it. Exactly one of:
   "news" = a report of something that happened, however small — arrests, \
rulings, votes, disasters, earnings, announcements, live updates
   "feature" = magazine-style writing rather than reporting: narrative \
longform, profiles, retrospectives, "the untold story of", human-interest \
storytelling about people rather than events
   "soft" = curiosity, lifestyle, celebrity gossip, viral oddities, \
service journalism ("what to know about", listicles, rankings)
   A reported event is "news" no matter how dramatic, tabloid, or minor \
it is — this tag is about the KIND of writing, not its importance or its \
subject. When unsure, choose "news".

Rules about people and legal matters — these override everything above, \
including the instruction to find a concrete stake. When a stake can only be \
stated by breaking one of these, return "" instead:
- Use only what the article states. Do not add facts, motives, or history that \
are not in the text you were given.
- Never characterize a legal outcome beyond what the source literally says. \
"Charged" is not "guilty." "Settled" is not "found liable." An investigation is \
not a finding. An accusation is not a fact.
- Attribute contested claims to whoever made them: "prosecutors say," "the \
complaint alleges," "according to <outlet>."
- A campaign contribution is not an endorsement. A committee seat is not a \
position. A vote is not a belief.
- Do not state a consequence for a named living person that the article treats \
as possible or alleged as though it were settled.

Articles:
{_build_articles_text(batch)}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, c=empty string (no additional evidence), s=score, t=tone, g=genre.
[{{"i":1,"c":"","s":3,"t":"neutral","g":"news"}}, ...]

Return ONLY the JSON array, no other text."""


# ---------------------------------------------------------------------------
# Live path (used by backfill scripts + as a manual fallback). Routine ingest
# uses the Batch API path below for the 50% discount.
# ---------------------------------------------------------------------------

async def generate_context(
    articles: list[dict],
    *,
    client: anthropic.AsyncAnthropic | None = None,
) -> dict[str, dict]:
    """
    Batch-classify importance, tone and genre; automatic significance stays empty.

    Input: list of dicts with keys: source_url, title, summary
    Output: dict mapping source_url -> {"context": str | None, "score": int,
    "tone": str}

    `context` is None when the quality gate drops the line (no real stake); the
    score and tone are still returned so callers can record them independently.
    """
    if not articles:
        return {}

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    results: dict[str, dict] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        batch_index = i // BATCH_SIZE
        try:
            results.update(await with_alignment_retry(
                functools.partial(_generate_batch, client, batch),
                logger=logger,
                event="context_batch_misaligned",
                batch_index=batch_index,
                ids=[a["source_url"] for a in batch],
            ))
        except AlignmentError as e:
            # Writing nothing leaves why_it_matters / importance_score NULL,
            # which the UI already tolerates and scripts/backfill_context.py
            # can regenerate. A line written against the wrong article is not
            # recoverable, because nothing downstream knows it is wrong.
            logger.error(
                "Context generation batch %d abandoned after %d misaligned attempts: %s",
                batch_index, MAX_BATCH_ATTEMPTS, e,
            )
        except Exception as e:
            logger.error("Context generation failed for batch %d: %s", batch_index, e)

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
        model=_model(),
        max_tokens=850,  # +tone (stage 3) and +genre (stage 6) keys
        messages=[{"role": "user", "content": _build_context_prompt(batch)}],
    )
    log_usage(OPERATION, response, model=_model())

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_context(text, batch)


def _parse_context(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's context + score response, applying the quality gate.

    The line and the score are decoupled: a line dropped by the gate (or returned
    empty by the model) still yields a row carrying the importance score, with
    context=None so the caller writes NULL why_it_matters.

    Raises AlignmentError unless the response carries exactly one entry per
    article — see services/index_alignment.py for why a range check alone is
    not enough. An EMPTY line is not a misalignment: the rubric asks for ""
    when there is no real stake (null-over-filler), so only the index
    structure is enforced here.
    """
    parsed = _extract_json_array(text)
    if parsed is None:
        raise AlignmentError("response was not a parseable JSON array")

    results: dict[str, dict] = {}
    for idx, item in aligned_entries(parsed, len(batch)).items():
        # Accept short keys (new) and fall back to long keys (legacy prompt form).
        score = item.get("s", item.get("score", 3))

        # Clamp score to 1-5 (always recorded, independent of the line).
        if not isinstance(score, int) or score < 1 or score > 5:
            score = 3

        article = batch[idx - 1]
        # No independent source beyond the summary; model compliance is not
        # required for this guarantee. Scores/classification remain independent.
        gated = None
        results[article["source_url"]] = {
            "context": gated,
            "score": score,
            "tone": _clamp_tone(item.get("t", item.get("tone"))),
            "genre": _clamp_genre(item.get("g", item.get("genre"))),
        }

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

    # Daily AI cost ceiling. Returning None is the existing "did not submit"
    # contract, so the caller already handles it: the articles simply go
    # without context for now and are backfillable. Cheap to degrade,
    # which is why the guard sits at submit time rather than in the result
    # handler — by then the money is spent.
    budget = await check_budget(estimate_cost(OPERATION, _batch_count(articles)))
    if not budget.allowed:
        logger.warning(
            "context: batch of %d articles not submitted (cost guard: %s)",
            len(articles), budget.reason,
        )
        return None

    requests: list[dict] = []
    for i in range(0, len(articles), BATCH_SIZE):
        sub = articles[i : i + BATCH_SIZE]
        custom_id = f"ctx-{i // BATCH_SIZE}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": _model(),
                "max_tokens": 850,  # matches _generate_batch
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
    """Store classification; withhold automatic notes without extra evidence.

    Queued batches from an older prompt are subject to the same rule.
    """
    # Batch spend was invisible until 2026-08-05 — this path recorded
    # nothing, leaving ~$1/day unattributed between the ledger and the bill.
    # Pass the resolved model: without it this defaults to the alias
    # "claude-haiku-4-5" while the realtime path above logs the dated snapshot,
    # so one stage wrote two model ids into ai_usage_daily for the same
    # physical model — and priced them apart the moment PRICES was keyed.
    log_batch_usage(OPERATION, results, model=_model())
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
    misaligned = 0
    for item in results:
        custom_id = item.get("custom_id", "")
        urls = custom_id_to_urls.get(custom_id, [])
        result = item.get("result", {})
        if result.get("type") != "succeeded":
            failed += 1
            continue

        if not urls:
            # No URL manifest for this custom_id — nothing can be mapped back.
            # Previously every entry just failed the range check silently.
            logger.error(
                "No metadata URLs for %s in batch %s; skipping sub-batch", custom_id, batch_id,
            )
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

        # Batch API results arrive asynchronously through the poller, so there
        # is no live request to re-ask: a sub-batch that cannot be proven
        # aligned is skipped whole. why_it_matters/importance_score stay NULL
        # (tolerated by the UI, regenerable via scripts/backfill_context.py);
        # a line written against the wrong article would not be recoverable.
        try:
            entries = aligned_entries(parsed, len(urls))
        except AlignmentError as e:
            misaligned += 1
            log_misaligned_sub_batch(
                logger,
                event="batch_context_misaligned",
                batch_id=batch_id,
                custom_id=custom_id,
                urls=urls,
                error=e,
            )
            continue

        # Ignore legacy/model-generated notes while retaining classification.
        pending: list[dict] = []
        for idx, entry in entries.items():
            score = entry.get("s", entry.get("score", 3))
            if not isinstance(score, int) or score < 1 or score > 5:
                score = 3

            url = urls[idx - 1]
            gated = None  # No additional source evidence for an automatic note.
            if gated is None:
                dropped += 1
            pending.append({
                "url": url, "line": gated, "score": score,
                "tone": _clamp_tone(entry.get("t", entry.get("tone"))),
                "genre": _clamp_genre(entry.get("g", entry.get("genre"))),
            })

        for p in pending:
            try:
                await pool.execute(
                    """
                    UPDATE articles
                       SET why_it_matters = $1,
                           importance_score = $2,
                           tone = $3,
                           genre = $4,
                           updated_at = NOW()
                     WHERE source_url = $5
                    """,
                    p["line"], p["score"], p["tone"], p["genre"], p["url"],
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
        "misaligned_sub_batches": misaligned,
    }))
