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

import functools
import json
import logging
from datetime import datetime, timezone

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
from services.quality_gate import gate_background
from services.usage_tracker import log_batch_usage, log_usage

logger = logging.getLogger("sift-api.primer_generator")

OPERATION = "primer_generator.batch"


def _model() -> str:
    """Resolved per call, never cached at import — an override must not need a
    restart to take effect, and a module constant would lie in tests."""
    return resolve(OPERATION).model


BATCH_SIZE = 5  # primer is more tokens out per article than one-liner context


def _batch_count(articles: list) -> int:
    return -(-len(articles) // BATCH_SIZE)


BATCH_KIND = "primer"  # identifier persisted to api_batches.kind

# Voice/format guard. Keep prompt-engineering tight: the LLM gets one job per
# article (write a primer + 0-4 terms) and outputs strict JSON. The prompt is
# the same in both the live and batch paths.
#
# Term-bar history: original prompt picked 3-5 terms with a soft "NOT common
# words" rule. In practice the LLM defaulted to safety and surfaced obvious
# vocabulary (tariffs, IPO, child support, pollen season, data center).
# Tightened in 2026-05-08 to 0-4 with a hard anti-pattern list — better to
# skip than to define an obvious word.
_PROMPT_HEADER = """You are writing the "What you should know first" panel for a civic-literacy news app. \
For each article below, write a brief teaching primer for a smart American adult who may have missed key context.

Two things per article:

1. background — ONE short paragraph (max 60 words) of context the reader \
needs before reading the article. Cover what's at stake, who the players are, or what came before — whichever \
the reader most likely doesn't already know. Conversational tone. Active voice. Contractions OK. \
NEVER editorialize, NEVER take a political side, NEVER tell the reader what to think. \
Avoid vague-significance clichés and filler — phrasings like "raises serious questions", "a turning point", \
"a wake-up call", "sends a message", or "remains to be seen" add no information. State concrete facts only; \
if you have no real background to add, return an empty string (better empty than filler).

2. terms — 0 to 4 key terms from the article that a college-educated reader \
would actually need to look up to understand the story. NOT proper nouns. Each term gets a max-25-word \
plain-language definition that an 8th-grader could understand.

The bar is HIGH. Better to return zero terms than to define an obvious word. Default to fewer terms.

GOOD picks (domain-specific jargon, procedural terms, regulatory or legal-of-art):
filibuster, cloture, basis points, antitrust review, FOMC, attainment standards, EBITDA, \
qualified immunity, federalism, deference doctrine, motion to proceed, Calendar Wednesday, \
chilling effect (when used in a 1A context), notice-and-comment, certiorari, prior restraint, \
adverse possession, force majeure, Section 230, indemnification, rulemaking, conferee.

DO NOT PICK these — they fail the "would a college-educated reader actually look this up?" bar. \
If you find yourself reaching for one of these, return fewer terms instead:

  • Common adult vocabulary: tariffs, IPO, child support, recession, supply chain, data center, \
utility, subscription, broadcast, social media, civil rights, immigration, federal agency, \
primary election, runoff, plea deal.
  • Generic English idioms: unintended consequences, downstream effects, chain reaction, \
knock-on effects, fallout, ripple effect, paradigm shift.
  • Tautological / circular definitions where the "definition" just restates the term: \
pollen season ("when pollen is released"), price hike ("when prices go up"), \
rate cut ("when rates are cut"), data center ("place that houses servers").
  • Words whose definition is one Google search away: typhoon, asteroid, vaccine, refinery, \
semiconductor, grid (electrical), GDP, moratorium.

Critical rules:
- Never use the word "context" in the primer itself.
- Never start with "This article is about" or similar meta-language.
- Never recommend a position or imply one is correct.
- When paraphrasing a third-party finding (a court ruling, regulator decision, panel determination, audit, study), \
preserve the source attribution: write "the court ruled the probe overstepped her authority" not "the probe \
overstepped her authority." The primer surfaces what's known to whom — never the LLM's own legal, political, \
or moral assessment of facts a third party determined.
- If the article is short or self-contained and needs no context, return background as an empty string \
and terms as an empty array. The UI hides empty primers.

Rules about people and legal matters — these override everything above, \
including the instruction to supply background. When background can only be \
written by breaking one of these, return "" instead:
- Your job is to add what the article assumes, so unlike a summary you may draw \
on well-established public record. That licence covers institutions, procedures, \
statutes, and history. It does NOT cover new claims about the conduct, motives, \
or state of mind of a living person named in the story. If the background you \
want to add is about a person rather than about a system, leave it out.
- Never characterize a legal outcome beyond what the source literally says. \
"Charged" is not "guilty." "Settled" is not "found liable." An investigation is \
not a finding. An accusation is not a fact. This binds background you supply \
from your own knowledge exactly as it binds the article's wording — recalling a \
later development the article does not mention is the likeliest way to break it.
- Attribute contested claims to whoever made them: "prosecutors say," "the \
complaint alleges," "according to <outlet>."
- A campaign contribution is not an endorsement. A committee seat is not a \
position. A vote is not a belief.
- Do not state a consequence for a named living person that the article treats \
as possible or alleged as though it were settled.
- A term definition is still prose about the story. Define the term, not the \
person or case it appears in: "qualified immunity" is a doctrine, never an \
assessment of whether this defendant deserves it.

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

async def generate_primers(
    articles: list[dict],
    *,
    client: anthropic.AsyncAnthropic | None = None,
) -> dict[str, dict]:
    """Generate primers for a list of articles via the live Messages API.

    Input: list of dicts with keys: source_url, title, summary, source_name (optional)
    Output: dict mapping source_url -> { background, terms, generated_at }

    For routine ingest, prefer submit_primer_batch (50% cheaper). This live path
    exists for backfill scripts and one-off jobs where async batch latency is
    unacceptable.
    """
    if not articles:
        return {}

    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
    results: dict[str, dict] = {}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        batch_index = i // BATCH_SIZE
        try:
            results.update(await with_alignment_retry(
                functools.partial(_generate_batch_live, client, batch),
                logger=logger,
                event="primer_batch_misaligned",
                batch_index=batch_index,
                ids=[a["source_url"] for a in batch],
            ))
        except AlignmentError as e:
            # Writing nothing leaves context_primer NULL — the UI hides the
            # panel, and scripts/backfill_primers.py can regenerate. A primer
            # attached to the wrong article teaches the reader background for
            # a story they are not reading.
            logger.error(
                "Primer generation batch %d abandoned after %d misaligned attempts: %s",
                batch_index, MAX_BATCH_ATTEMPTS, e,
            )
        except Exception as e:
            logger.error("Primer generation failed for batch %d: %s", batch_index, e)

    logger.info("Generated primers for %d/%d articles", len(results), len(articles))
    return results


async def _generate_batch_live(
    client: anthropic.AsyncAnthropic,
    batch: list[dict],
) -> dict[str, dict]:
    response = await client.messages.create(
        model=_model(),
        max_tokens=1500,  # ~300 tokens per article * 5 articles + headroom
        messages=[{"role": "user", "content": _build_prompt(batch)}],
    )
    log_usage(OPERATION, response, model=_model())

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_primers(text, batch)


def _parse_primers(text: str, batch: list[dict]) -> dict[str, dict]:
    """Parse Claude's primer JSON response into the canonical persisted shape.

    Raises AlignmentError unless the response carries exactly one entry per
    article — see services/index_alignment.py for why a range check alone is
    not enough. An entry that comes back fully empty is still an entry: only
    the index structure is enforced here, and empty payloads are skipped
    below as before.
    """
    parsed = _extract_json_array(text)
    if parsed is None:
        raise AlignmentError("response was not a parseable JSON array")

    results: dict[str, dict] = {}
    now_iso = datetime.now(timezone.utc).isoformat()
    for idx, item in aligned_entries(parsed, len(batch)).items():
        background = item.get("b", item.get("background", "")) or ""
        terms_raw = item.get("t", item.get("terms", [])) or []

        # Cliché-gate the background (sift-api#90). Lighter touch than
        # why_it_matters: clichés only, never restatement — and terms are kept
        # regardless (they're the differentiated value). An "" background just
        # hides the paragraph.
        article = batch[idx - 1]
        background = gate_background(
            background, title=article.get("title", ""), summary=article.get("summary", ""),
        )

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

        results[article["source_url"]] = {
            "background": background,
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

    # Daily AI cost ceiling. Returning None is the existing "did not submit"
    # contract, so the caller already handles it: the articles simply go
    # without primer for now and are backfillable. Cheap to degrade,
    # which is why the guard sits at submit time rather than in the result
    # handler — by then the money is spent.
    budget = await check_budget(estimate_cost(OPERATION, _batch_count(articles)))
    if not budget.allowed:
        logger.warning(
            "primer: batch of %d articles not submitted (cost guard: %s)",
            len(articles), budget.reason,
        )
        return None

    requests: list[dict] = []
    for i in range(0, len(articles), BATCH_SIZE):
        sub = articles[i : i + BATCH_SIZE]
        custom_id = f"primer-{i // BATCH_SIZE}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": _model(),
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
    # Batch spend was invisible until 2026-08-05 — this path recorded
    # nothing, leaving ~$1/day unattributed between the ledger and the bill.
    # Pass the resolved model — see the note in context_generator: omitting it
    # logged the alias here and the dated snapshot on the realtime path.
    log_batch_usage(OPERATION, results, model=_model())
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
    bg_dropped = 0
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

        # No live request to re-ask here (results arrive via the poller), so a
        # sub-batch that cannot be proven aligned is skipped whole:
        # context_primer stays NULL and scripts/backfill_primers.py can
        # regenerate it. A primer on the wrong article is not recoverable.
        try:
            entries = aligned_entries(parsed, len(urls))
        except AlignmentError as e:
            misaligned += 1
            log_misaligned_sub_batch(
                logger,
                event="batch_primer_misaligned",
                batch_id=batch_id,
                custom_id=custom_id,
                urls=urls,
                error=e,
            )
            continue

        now_iso = datetime.now(timezone.utc).isoformat()
        for idx, entry in entries.items():
            background = entry.get("b", entry.get("background", "")) or ""
            terms_raw = entry.get("t", entry.get("terms", [])) or []

            # Cliché-gate the background (sift-api#90). Cliché-only — needs no
            # title/summary — so no extra DB read here; terms are kept regardless.
            raw_bg_present = bool(background.strip())
            background = gate_background(background)
            if raw_bg_present and not background:
                bg_dropped += 1

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
                "background": background,
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
        "backgrounds_dropped_by_gate": bg_dropped,
        "failed": failed,
        "misaligned_sub_batches": misaligned,
    }))
