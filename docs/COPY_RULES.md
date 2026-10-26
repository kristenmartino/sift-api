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

Moving an article to a different story atomically invalidates the old story's
synthesis and resets its retry count. A source can no longer leave an old cluster
while its figures or notes remain published there. The normal bounded sweep
resynthesizes the remaining members; the destination is not invalidated by its
own membership update.

A halftime score must remain an interim lead, not a completed win. Both article
and story generation reject the observed “defeated … 33-6 at halftime” wording;
the article batch re-asks and the cluster remains retryable. Final wins and
explicit halftime leads are retained.

## Source-fidelity review (2026-10-25.3)

Primary article summaries and unified cluster text now receive a budget-checked
Sonnet review against the actual inputs supplied to generation. It checks named
people, spelling, relationships, attribution, uncertainty and concrete content,
including generic wording inherited from older member summaries. Concise copy
may omit source details; omission alone is not failure. This reviews fidelity to
inputs, not the independent factual truth of a publisher's claims.

A failed or unavailable article review blanks the candidate and leaves durable
generation pending. An explicit source-only guide verdict is unavailable until
changed usable source text arrives. A failed cluster review remains retryable
and unpublished. The configured daily cap and event-driven scheduling remain.

The Section 338 definition uses a small cited override in
`services/term_references.py`, based on 19 U.S.C. 1338, instead of allowing a
generic national-security tariff description. Both real-time and batch primer
results pass through that parser. The override is not a general legal-reference
database.

`scripts/audit_source_copy.py --regressions --daily-cap 15` runs labeled failures
and useful-copy controls through the actual reviewer. Without `--regressions`,
it checks a bounded recent production sample; it does not modify content.
Its guard is always enabled, with the environment's configured cap unless an
operator explicitly provides one. Review errors are failures, not silent passes.

Background prose uses the same usefulness bar as glossary entries. It cannot
reintroduce obvious vocabulary as a paragraph or infer player-acquisition /
postseason-eligibility guarantees from a trade deadline. Useful institutional
roles and unfamiliar procedures remain retention controls.

## Post-release input fidelity (2026-10-26.1)

Article and cluster checks reject ambiguous Bolsonaro first-name guesses and
the observed Jake LaRavia-to-LaRiver spelling drift. Reviews receive the publisher name so a properly
attributed editorial is not mistaken for an invented attribution. Review also
checks causal reversals, disputed-claim actors and unattributed rankings.

The summary gate trims generic rivalry/significance tails. Framing checks
withhold undated no-arrests notes when the current summary reports an arrest,
while retaining explicitly dated earlier states.

Both primer definitions and paragraphs reject unsourced figures, spelled-out
term lengths, mortgage-share fractions, named-law claims and categorical
healthy-tissue guarantees. Semantic checks distinguish federal prosecutors
from Senate-confirmed U.S. Attorneys and require precise period definitions.
Stable institutional roles and useful jargon remain retention controls.

The same release also checks spelled-out counts beyond term lengths and asks
background review to withhold unverified league rules, financial guarantees,
religious practices and institutional powers. Familiar runoff definitions do
not earn a background paragraph. A record teaser with no underlying record is
withheld rather than rewritten as a vague historical achievement.

An additional labeled control caught a level-funded-plan claim that employers
pay only actual claims rather than fixed premiums. The deterministic primer
guard withholds that unsupported guarantee even when semantic review accepts
it. `scripts/audit_primer_copy.py --daily-cap 15` replays the observed primer
failures and useful-copy controls through both the guard and reviewer.

The name backstop is deliberately scoped: fuzzy capitalized-word matching
misclassified company plurals and title-cased verbs as changed surnames in the
next live cohort. Those controls now stay clear; general name fidelity remains
the semantic reviewer's job.

Recovered-article controls also preserve after/during timing, distinguish a
pledge to act from a commitment to impeach, and reject invented speakers such
as unnamed party strategists. Each observed failure has a supported-copy
retention pair in the source-fidelity replay.

Review evidence now includes the headline alongside the excerpt. Live checks
showed that separate-title-only facts were sometimes ignored. Missing-record
and missing-poll-result teasers are explicitly unavailable; meaningful reported
results still pass.

## Pending-batch recovery (2026-10-26)

The numeric guard now treats compound spelled-out counts below 100 and digits
as equivalent, including percent units and scale words. “Forty-seven” supports
47, not 7 or 48. A real source count had been rejected and held up four articles.

After bounded batch retries, a content-gate failure is retried one article at a
time. A rejected neighbor remains pending while other articles continue to
source review. Duplicate, missing or shifted indices still reject the entire
batch; content quality never relaxes the alignment requirement.

## Repairable source-review holds (2026-10-26)

Neutral paraphrase can remove rhetorical degree: “demolished” supports
“defeated.” This does not relax legal status, dates or attribution. Interpretive
claims about a song accompanying a post remain attributed to the outlet.

A production candidate rejected as retryable may receive one budget-checked
rewrite using the supplied source and reviewer feedback. The rewrite must
pass the parser's factual checks and a fresh independent source review before
publication. Unchanged text is not re-reviewed; unavailable inputs, review
errors, malformed repairs and failed second reviews remain withheld. There is
no recursive rewrite loop and no new polling or UI behavior.

An invalid, truncated or misaligned review response is re-requested once, with
a separate budget check; a second failure stays an error and cannot authorize
publication. Availability is decided from all supplied source facts, not from
the rejected candidate: an invented record with a real game result calls for
a rewrite, rather than declaring the whole source unavailable.

## Audit of the automatic path (2026-10-26)

Producer-declared unavailable outputs now receive a separate source-availability
review before becoming unavailable. If the source contains usable news, the
normal one-rewrite path must produce text and obtain a fresh copy review. A
blank candidate cannot pass; conflicting availability decisions stay pending.
Sources with no usable body remain withheld.

Cluster audits use the same publisher-labeled member evidence as synthesis,
avoiding false invented-attribution flags when an outlet name exists only in
metadata. The summary gate trims the observed kickoff/TV-release teaser tail
and unnamed-opponent score placeholder while retaining quoted text and named
opponents. No timers, UI or ranking changes accompany these corrections.

Source review also checks cluster framing notes, preserves active-season timing,
rejects invented outlet disagreements and scheduling causes, and drops ambiguous
price-discount claims. Availability repairs explicitly use the identified fact
while preserving literal source figures.

2026-10-26 scheduled-run follow-up: refresh clusters after same-outlet
attachments, include publication dates in shared source evidence, and prefer
later confirmed resolutions over superseded uncertainty. Failed attachment
refreshes are withheld and queued for the scheduled sweep. Vague performance,
analytics-effort and ranking-change teasers with no actual finding are
unavailable. Repair review outcomes are logged alongside attempts.

Confirmed updates also advance cluster publication time from actual member
publication dates. Explicit retail discount percentages remain valid when
supplied in a title; the ambiguous property-discount rule must not remove them.

2026-10-26 event integrity: similarity proposes candidates, not membership.
Validate the full proposed group before attachment/creation; reject topic-only
roundups, different games and promotional bridges. Incoherent failed groups
stay unpublished and stop consuming synthesis retries. Story-copy review also
checks full membership; retaining one factual sentence cannot validate a mixed group.
