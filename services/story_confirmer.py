"""One batched LLM call that confirms which kNN candidates are the same event.

WHAT IT REPLACES
----------------
`services/story_clusterer.cluster_articles` sends up to 50 articles per
category per run and asks Claude to partition them from scratch — ~5.4 calls
per run, plus ~23 `synthesize_story` calls behind them. Threading was 43% of
Anthropic spend.

`services/story_matcher` has already done the hard part for free: everything
reaching this module is a pair Postgres scored at >= 0.60 cosine. The only
question left is the one embeddings genuinely cannot answer — **same topic or
same event?** Two Fed-rate stories a week apart embed close together and are
not the same event. That distinction is `topic_conflation_rate` in
`services/cluster_metrics.py`, and it is why this call still exists.

So: one call per run, across all categories, over pre-filtered candidates,
instead of one partition-from-scratch call per category.

ALIGNMENT IS ENFORCED, NOT ASSUMED
----------------------------------
Every indexed batch response in this repo goes through
`services/index_alignment` after #117, where a summary was attached to the
wrong article because the response was trusted to line up with its input. The
same failure here would attach an article to another article's story, which is
worse — it is visible to readers as "how N outlets covered this."
"""
from __future__ import annotations

import json
import logging
from typing import TypedDict

import anthropic

from app.config import settings
from services.index_alignment import AlignmentError, aligned_entries, with_alignment_retry
from services.usage_tracker import log_usage

logger = logging.getLogger("sift-api.story_confirmer")

MODEL = "claude-haiku-4-5-20251001"

# Output is one short object per candidate. 60 tokens each is generous for
# {"i":N,"action":"...","story_id":"..."}; the floor covers small runs.
MAX_OUTPUT_TOKENS_BASE = 256
MAX_OUTPUT_TOKENS_PER_CANDIDATE = 60
MAX_OUTPUT_TOKENS_CEILING = 4096

# Candidates per call. The prompt carries ~120 tokens per candidate, so 40 is
# ~5k input — comfortably inside a single request, and a full run is normally
# well under this.
BATCH_SIZE = 40


class Decision(TypedDict):
    action: str          # "attach" | "new" | "none"
    story_id: str | None  # set when action == "attach"
    members: list[str]    # article ids, set when action == "new"


def _max_tokens(n: int) -> int:
    return min(
        MAX_OUTPUT_TOKENS_CEILING,
        MAX_OUTPUT_TOKENS_BASE + MAX_OUTPUT_TOKENS_PER_CANDIDATE * n,
    )


def _prompt(batch: list[dict]) -> str:
    """Render candidates as numbered blocks with explicit, enumerated options.

    Enumerating the options rather than asking for free-form grouping is
    deliberate: the model picks from a closed set, so a response is either
    valid or provably invalid. There is no partition to misparse.
    """
    blocks = []
    for i, c in enumerate(batch, start=1):
        a = c["article"]
        lines = [
            f'{i}. NEW: "{a["title"]}" ({a.get("source_name") or "unknown"})',
            f'   {(a.get("summary") or "")[:240]}',
            "   Options:",
        ]
        for sid, members in c["existing_stories"].items():
            names = ", ".join(
                f'"{m["title"][:70]}" ({m.get("source_name") or "?"})' for m in members[:3]
            )
            lines.append(f'     - attach to story {sid}: {names}')
        if c["loose_neighbours"]:
            names = "; ".join(
                f'[{m["id"]}] "{m["title"][:70]}" ({m.get("source_name") or "?"})'
                for m in c["loose_neighbours"][:5]
            )
            lines.append(f"     - new story with any of: {names}")
        lines.append("     - none")
        blocks.append("\n".join(lines))

    return f"""You are deciding which news articles cover THE SAME EVENT.

Each numbered item is a newly ingested article, followed by candidate matches
that a vector search already found to be closely related. Related is not
enough. Two articles about Fed rate policy a week apart are the same TOPIC and
different EVENTS; only the same event belongs in one story.

For each item choose exactly one:
  - attach to an existing story, if the new article covers that same event
  - a new story, listing the article ids that cover the same event as it
  - none, if nothing here is the same event

{chr(10).join(blocks)}

Return ONLY a JSON array, one object per item, using these exact shapes:
  {{"i": 1, "action": "attach", "story_id": "<id>"}}
  {{"i": 2, "action": "new", "members": ["<article id>", ...]}}
  {{"i": 3, "action": "none"}}

Include every index from 1 to {len(batch)} exactly once. No prose."""


def _parse(text: str, batch: list[dict]) -> dict[int, Decision]:
    """Decode, prove alignment, then validate each choice against its options.

    A hallucinated story id or member id is rejected rather than written. The
    model can only pick from what it was offered; anything else degrades that
    one item to "none" instead of corrupting a story.
    """
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise AlignmentError("no JSON array in response")
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AlignmentError(f"undecodable JSON array: {e}") from e
    if not isinstance(parsed, list):
        raise AlignmentError("response JSON is not an array")

    by_index = aligned_entries(parsed, len(batch))

    out: dict[int, Decision] = {}
    for idx, entry in by_index.items():
        c = batch[idx - 1]
        action = entry.get("action")

        if action == "attach":
            sid = entry.get("story_id")
            if sid in c["existing_stories"]:
                out[idx] = Decision(action="attach", story_id=sid, members=[])
                continue
            logger.info(json.dumps({
                "event": "confirmer_rejected_choice",
                "reason": "story_id not among this item's options",
                "index": idx, "story_id": sid,
            }))

        elif action == "new":
            offered = {m["id"] for m in c["loose_neighbours"]}
            members = [m for m in (entry.get("members") or []) if m in offered]
            if members:
                out[idx] = Decision(action="new", story_id=None, members=members)
                continue
            logger.info(json.dumps({
                "event": "confirmer_rejected_choice",
                "reason": "no offered member ids in 'new' choice",
                "index": idx, "members": entry.get("members"),
            }))

        out[idx] = Decision(action="none", story_id=None, members=[])

    return out


async def confirm(
    candidates: list[dict],
    *,
    client: anthropic.AsyncAnthropic | None = None,
    model: str = MODEL,
) -> dict[str, Decision]:
    """Confirm candidates, returning {article_id: Decision}.

    Degrades to "none" for a whole sub-batch that cannot be proven aligned
    after retries — writing nothing is the safe failure here, because the
    articles stay marked threaded but unattached, and a later arrival can
    still pull them into a story via kNN. Nothing is lost permanently.
    """
    if not candidates:
        return {}

    client = client or anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key, max_retries=2,
    )
    results: dict[str, Decision] = {}

    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        ids = [c["article"]["id"] for c in batch]

        async def _call(batch=batch) -> dict:
            resp = await client.messages.create(
                model=model,
                max_tokens=_max_tokens(len(batch)),
                messages=[{"role": "user", "content": _prompt(batch)}],
            )
            log_usage("story_confirmer.confirm", resp, model=model)
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            return _parse(text, batch)

        try:
            decided = await with_alignment_retry(
                _call, logger=logger, event="confirm_batch_misaligned",
                batch_index=start // BATCH_SIZE, ids=ids,
            )
        except AlignmentError:
            logger.error("confirmer: sub-batch %d unaligned after retries — no writes",
                         start // BATCH_SIZE)
            decided = {}
        except Exception as e:  # noqa: BLE001 — threading must not break ingest
            logger.error("confirmer: sub-batch %d failed (%s) — no writes",
                         start // BATCH_SIZE, e)
            decided = {}

        for i, c in enumerate(batch, start=1):
            results[c["article"]["id"]] = decided.get(
                i, Decision(action="none", story_id=None, members=[])
            )

    return results
