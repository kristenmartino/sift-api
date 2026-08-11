"""Prove that an indexed LLM response lines up with the batch it was asked about.

Every batched call in this repo asks the model for a JSON array carrying a
1-based index per input item, then maps results back through that index:

    results[batch[idx - 1]["source_url"]] = ...

Checking only that `idx` is in RANGE is not enough. A repeated, skipped, or
shifted index passes a range check and silently attaches a result to the WRONG
article. That is confirmed production behaviour, not a hypothetical: articles
were found carrying other articles' summaries on 2026-07-30, written by a path
that reported success. Same failure class as the missing `zip(..., strict=True)`
in workflows/pipeline_workflow.py (#113) — a batch response trusted to line up
with its input instead of being made to prove it.

So a response is accepted only when its indices are exactly {1..n}: no
duplicates, no gaps, nothing out of range. Anything else raises AlignmentError
and the caller either re-asks (live paths, via with_alignment_retry) or writes
nothing for that sub-batch (Batch API paths, whose results arrive
asynchronously through the poller and cannot be re-asked in place).

Why all-or-nothing rather than keeping the entries that look fine: a gap is
indistinguishable from a shift. If a batch of 5 comes back with indices
1,2,3,4, either the model skipped article 5 (the other four are right) or it
shifted everything by one (all four are wrong). Nothing in the response tells
you which, so none of it can be trusted.

Scope is index integrity ONLY. Whether a PAYLOAD is acceptable — an empty
summary, a gated why_it_matters line, a primer with no terms — is
domain-specific and stays with each caller.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

# A misaligned batch is re-asked rather than written. Sampling is non-zero
# temperature, so the retry is a genuinely different draw. 2 = one retry;
# misalignment is rare, so the added cost is a rounding error against the
# per-run spend. Only applies to live paths — see with_alignment_retry.
MAX_BATCH_ATTEMPTS = 2


class AlignmentError(RuntimeError):
    """A batch response could not be proven to line up with its input batch.

    Raised instead of writing a result that might belong to a different
    article. See aligned_entries for what "proven" means here.

    The three response attributes below are filled in by callers that have the
    response in hand (`aligned_entries` itself only sees the decoded JSON).
    They exist because "the batch misaligned" does not say *why*, and one
    likely why is cheap to test: a response cut off at `max_tokens` is
    truncated JSON, which fails alignment exactly the way a genuinely
    scrambled response does — but the fix is a bigger ceiling, not a re-ask.
    """

    stop_reason: str | None = None
    output_tokens: int | None = None
    max_output_tokens: int | None = None


def aligned_entries(parsed: list[Any], expected: int) -> dict[int, dict]:
    """Return {index: entry} covering exactly 1..expected, or raise AlignmentError.

    `parsed` is the decoded JSON array from the model. Entries may carry the
    short key `i` (current prompts) or the long key `index` (legacy prompt
    form); both are accepted, mirroring the payload key handling at each call
    site. Order is irrelevant — the index, not the position, carries the
    mapping — so an out-of-order but complete response is fine.
    """
    by_index: dict[int, dict] = {}

    for entry in parsed:
        if not isinstance(entry, dict):
            raise AlignmentError(f"non-object entry in response: {entry!r}")

        idx = entry.get("i", entry.get("index"))
        # bool is an int subclass; True would otherwise sail through as index 1.
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise AlignmentError(f"non-integer index {idx!r}")
        if not 1 <= idx <= expected:
            raise AlignmentError(f"index {idx} outside 1..{expected}")
        if idx in by_index:
            raise AlignmentError(f"duplicate index {idx}")

        by_index[idx] = entry

    wanted = set(range(1, expected + 1))
    if set(by_index) != wanted:
        missing = sorted(wanted - set(by_index))
        raise AlignmentError(
            f"got {len(by_index)} entries for {expected} inputs, missing indices {missing}"
        )

    return by_index


async def with_alignment_retry(
    call: Callable[[], Awaitable[dict]],
    *,
    logger: logging.Logger,
    event: str,
    batch_index: int,
    ids: list[str],
    attempts: int = MAX_BATCH_ATTEMPTS,
) -> dict:
    """Run `call`, re-running it while it raises AlignmentError.

    Only AlignmentError is retried. Transport/API errors are already retried
    inside the SDK client and are left to the caller's own fallback.

    Each attempt emits a structured event carrying the batch's ids, so a
    misalignment can be traced back to specific articles after the fact — the
    production cases found 2026-07-30 were invisible because this path
    reported success. Re-raises the last AlignmentError once attempts run out.

    Live paths only. Batch API results arrive asynchronously through the
    poller with no live request to repeat; those callers use aligned_entries
    directly and skip the sub-batch instead.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except AlignmentError as e:
            final = attempt == attempts
            logger.info(json.dumps({
                "event": event,
                "batch_index": batch_index,
                "attempt": attempt,
                "max_attempts": attempts,
                "batch_size": len(ids),
                "final": final,
                "reason": str(e),
                # How the call ended. "max_tokens" here means the response was
                # truncated mid-array, so the re-ask is treating a too-small
                # ceiling as a scrambled answer — raise the ceiling instead.
                "stop_reason": e.stop_reason,
                "output_tokens": e.output_tokens,
                "max_output_tokens": e.max_output_tokens,
                "source_urls": ids,
            }))
            logger.warning(
                "Batch %d misaligned (attempt %d/%d): %s",
                batch_index,
                attempt,
                attempts,
                e,
            )
            if final:
                raise


def log_misaligned_sub_batch(
    logger: logging.Logger,
    *,
    event: str,
    batch_id: str,
    custom_id: str,
    urls: list[str],
    error: Exception,
) -> None:
    """Structured record of a Batch API sub-batch dropped for misalignment.

    The Batch API path cannot re-ask, so the sub-batch is skipped entirely and
    its columns stay NULL. This event is what makes that recoverable: it names
    every affected source_url, and the matching scripts/backfill_*.py can
    regenerate them.
    """
    logger.error(json.dumps({
        "event": event,
        "batch_id": batch_id,
        "custom_id": custom_id,
        "batch_size": len(urls),
        "reason": str(error),
        "source_urls": urls,
    }))
