# Feed copy rules

The common generation standard lives in `services/copy_rules.py`. Its version
is returned as `copy_rules_version` by `/health`. Every article summarizer,
significance-note generator, primer generator, and story synthesizer includes
the same standard. Changing a model does not remove it.

- Report concrete source-supported facts directly; one useful sentence is enough.
- Omit general closing observations about importance, tensions, or implications.
- Preserve figures, uncertainty, named attribution, and unresolved causality.
- Optional notes must add information. Empty is the correct result when they do not.
- Background explains a specific mechanism or procedure beyond the news; terms
  must be useful, with at most four definitions of at most 25 words each.
- Coverage notes describe a distinct supplied detail in at most 25 words. An
  excerpt cannot prove that the full article omitted a fact.
- Keep real sources even when their notes are blank; do not invent sources or
  create multiple rows for repeated articles from the same supplied source name.
  The frontend resolves publisher aliases separately.

## Enforcement

`services/quality_gate.py` removes recognizable generic summary closers,
unsupported omission claims, cliché notes, and near-verbatim repetition. A new
figure is protected from the repetition check. Quotes and attributed findings
have retention controls in the regression suite. Overlength optional notes are
omitted rather than truncated, which could cut off a qualification.

Both real-time and scheduled primer results use `_parse_primers`. Scheduled
results fetch the matching article reference once on completion, before writing.
If that reference cannot be read, optional primer copy is withheld. Context
results similarly withhold unchecked significance notes while retaining scores.
Neither change introduces an idle database poll or a new model call.

The shared story synthesizer covers initial clusters, new-outlet updates, and
failed-synthesis retries. It gates the summary and builds framing rows from the
actual source inputs, fills missing sources with blank notes, and ignores
invented or duplicate source rows. `story_copy_gated` logs the rule version and
result counts.

## Regression checks

Run `pytest tests/` and `ruff check .` before merging. CI runs the full suite on
every PR and push to main. `data/eval/feed_copy_regressions.json` holds exact
failures found in the October 24 live review. `tests/test_durable_copy_rules.py`
replays them and exercises the actual article parser, asynchronous result
handlers, and story synthesizer, with controls for useful copy and source links.

These are generation rules and conservative deterministic checks, not a claim
that regex can validate every fact or detect every semantic paraphrase. The
existing optional LLM judge remains off by default; its evaluation requirements
and cost guard have not changed. New failure patterns should be added with a
failure fixture and a retention control before expanding a filter. Historical
records are not rewritten by these safeguards.
