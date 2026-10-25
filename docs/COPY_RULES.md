# Feed copy rules

The common generation standard lives in `services/copy_rules.py`. Its version
is returned as `copy_rules_version` by `/health`. Every article summarizer,
significance-note generator, primer generator, and story synthesizer includes
the same standard. Changing a model does not remove it.

- Report concrete source-supported facts directly; one useful sentence is enough.
- Omit general closing observations about importance, tensions, or implications.
- Preserve figures, uncertainty, named attribution, and unresolved causality.
- Optional notes must add information. Automatic significance notes stay blank
  because that stage has only a headline and summary, with no additional evidence.
- Background explains a specific mechanism or procedure beyond the news; terms
  must occur in the supplied headline/summary and be useful, with at most four
  definitions of at most 25 words each.
- Coverage notes copy a distinct contiguous source passage in at most 25 words. An
  excerpt cannot prove that the full article omitted a fact.
- Keep real sources even when their notes are blank; do not invent sources or
  create multiple rows for repeated articles from the same supplied source name.
  The frontend resolves publisher aliases separately.

## Enforcement

`services/quality_gate.py` removes recognizable generic summary closers,
unsupported omission claims, cliché notes, and near-verbatim repetition. New
summary figures must appear in their supplied inputs; an invented figure leaves
story synthesis retryable or re-asks the article batch. A new
figure is protected from the repetition check. Quotes and attributed findings
have retention controls in the regression suite. Overlength optional notes are
omitted rather than truncated, which could cut off a qualification.

Both real-time and scheduled primer results use `_parse_primers`. Scheduled
results fetch the matching article reference once on completion, before writing.
If that reference cannot be read, optional primer copy is withheld. Context
results always withhold automatic significance notes while retaining scores.
Definitions pass a focused `term_utility` review. Obvious vocabulary and circular
definitions are withheld. Background review receives retained definitions and
rejects semantic duplication or article-specific conditions absent from its inputs.
Nonempty background paragraphs also pass a focused `background_novelty` check
through the existing Sonnet judge operation. A failed or budget-blocked check
blanks the paragraph while retaining useful definitions. Neither path introduces
an idle database poll.

The shared story synthesizer covers initial clusters, new-outlet updates, and
failed-synthesis retries. It gates the summary and builds framing rows from the
actual source inputs, fills missing sources with blank notes, and ignores
invented or duplicate source rows. `story_copy_gated` logs the rule version and
result counts. Surviving source passages also receive a focused semantic
redundancy review through `judge_lines(field="framing")`. It uses the existing
Sonnet judge operation and cost ledger. If the cost guard blocks it, the result
is malformed, or the reviewer cannot approve a note, that note stays blank.
These are paid checks for optional paragraph/notes novelty, not a general
factuality certification.

## Regression checks

Run `pytest tests/` and `ruff check .` before merging. CI runs the full suite on
every PR and push to main. `data/eval/feed_copy_regressions.json` holds exact
failures found in the October 24 live review. `tests/test_durable_copy_rules.py`
replays them and exercises the actual article parser, asynchronous result
handlers, and story synthesizer, with controls for useful copy and source links.

These are generation rules and conservative deterministic checks, not a claim
that regex can validate every fact or detect every semantic paraphrase. The
general LLM judge remains available for offline evaluation. The focused source-
note redundancy check above runs automatically; the legacy significance-judge
flag cannot enable notes without additional evidence. New failure patterns should be added with a
failure fixture and a retention control before expanding a filter. Historical
records are not rewritten by these safeguards.

## Recovery and provenance

Rules 2026-10-25.2 retain source text for durable retries after budget/provider
failure. Pending summaries are retried on the ingestion cadence, not a separate
DB timer. Headline-only or pure promotional/guide inputs are explicitly unavailable.
`/health.generation` distinguishes this queue from successful RSS ingestion.
Stories record the rules version and only reuse current-version text. Numeric
checks allow equivalent scales (7m, 7 million, 7,000,000) while rejecting changed
values; this remains a figure-evidence check, not verification of a whole claim.

Retry evidence survives an empty or headline-only RSS update. Unavailable entries
are reconsidered only when the publisher provides changed, usable content; an
unchanged guide does not repeatedly consume generation budget. Successful retries
of both pending and unavailable entries rejoin story threading.

An explicit grammatical reversal of “A nears a takeover of B” into “A's takeover
by B” is rejected by both article and cluster generation. The check preserves
correct active/passive wording and denied claims; it is not a general transaction
or factuality validator. Prompts also require buyer/target roles and talks to
remain as supplied.
