# sift-api — STATUS

**Updated:** 2026-08-18
**Tier:** v1.5 (civic-literacy pivot backend) — **feature work active**; the D46 pause was lifted 2026-08-05 ([`sift/docs/DECISIONS.md` D46](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md), amended). Android stays paused and the week-one evidence test is still the next action — what was withdrawn is the blanket prohibition on building, not those.
**Velocity:** Resumed 2026-07-30 after a six-week gap (last prior commit 2026-06-17; Jun 13 · Jul 0 until today). **2026-07-31: 8 PRs merged** (#116, #117, #120, #121, #123, #124, #126, #127) — a burst, not a new baseline. This line read "High (10+ PRs / week)" until 2026-07-30 and had been wrong for ~8 weeks — the same staleness `sift/STATUS.md` already corrected on its own copy 2026-07-27. Keep the two in step.

## Active focus

**Pipeline honesty — the "reports success while producing nothing" failure class.** #113 (2026-07-30) found it in clustering. 2026-07-31 found it in three more places, all with the same shape: a step fails, the failure is swallowed or unverified, and the run logs success.

| Where | It looked fine while… | Now |
|---|---|---|
| clustering (#113) | a truncated response produced zero stories | `cluster_stats` per run |
| summaries (#117) | summaries were attached to the **wrong articles** | indices proven `{1..n}`, else re-asked |
| RSS feeds (#122–#124) | 4 of 58 feeds contributed nothing, one for its whole life | `feed_stats` per run |
| outlet output (#125–#126) | a live feed served the same items forever | `feed_health` daily |
| summaries again (#118) | the model's apology was the card copy | refusal gate at write time |
| entity linking, write path (#136) | an 8s-timeout wave cleared 218 rows to `[]` | failure is `None`, not an empty answer |
| entity linking, read path (#139) | the regex fallback was unreachable for a per-article timeout | `link_articles` passes `omit_failures=True` |

All four signals verified live in prod 2026-07-31: `feed_stats` 59/59 · `feed_health` 56/56 · `Summarized 60/61 (0 batches fell back)` · `Skipping 1/61 articles whose RSS entry carries no body text`.

**The lesson worth carrying** is not any single bug: it is that **every one was found by hand, weeks late, by someone looking at something else.** Two of the fixes were themselves wrong on the first attempt and were only corrected by replaying them against known-true cases (#120's triage ranked the one confirmed swap *last*; #126's first rule paged on WHO behaving normally). A detector that has never been run against a known-true case is an untested detector — same shape as #113's meta-suite caveat.

Three things are now waiting on data rather than on code:

1. ~~**Cost attribution.**~~ **Answered 2026-08-05, saving verified 2026-08-10** — breakdown in Open-Q #1. It settled both questions it was gating: batching the entity linker is superseded by the regex gate, and raising `LIMIT 50` is the wrong fix (the window, not the limit, is the constraint). **Measured: $5.38 → $3.24 per 1k articles, −39.8%**, on the first window containing no legacy threading at all. Threading alone is **$2.34 → $1.11 per 1k, −52.5%**. This line said "the *saving* still is not [confirmed]" for five days; that is now retired. Always re-baseline from `scripts/verify_cost_baseline.py`, never from `linker_gate_stats` or by hand.
2. ~~**Clustering eval corpus.**~~ **Measured 2026-08-13.** The corpus is labeled (300 articles, 90 carrying an `event_id`) and the baseline is recorded over 15 repeats: **ARI 0.538, pairwise F1 0.779, multi-outlet precision 0.607.** That replaces the retracted "~97% accuracy" claim — the real number is nowhere near it. **Read it with the caveat that the corpus labels have never been human-validated:** `data/eval/review_pairs.csv` still holds 40 blind pairs with 0 verdicts filled, and the corpus records no annotator provenance, so this measures agreement with the labels, not with the truth. Cohen's kappa on those 40 pairs is ~20 minutes of labeling and is what would settle it.
3. ~~**Intra-batch duplicate source_urls**~~ (#145) — **measured 2026-08-13: 22 of 581 fetched articles, 3.79%.** Not zero, and not the 1-in-33 the first inference suggested. The cause is now named: outlets with **section feeds publish the same URL to both** — NPR / NPR World / NPR Health, The Hill / The Hill Politics. Dedup keys on `content_hash` and has no intra-batch `source_url` rule, so both copies are summarized *and* entity-linked at full price before collapsing at store time via `ON CONFLICT (source_url)`. At ~$1.33/1k across the per-article stages that is roughly **$3/mo of pure waste**, and the fix is one line beside the existing `content_hash` check. Found sideways, by `scripts/eval_summarizer.py --sample` reporting "150 scored" against runs that had summarized 147.

Still open from the prior focus: topic search → sift-api (#79/#80), NULL-embedding repair (#76 — now a prerequisite, not a nice-to-have, since `story_workflow` excludes NULL-embedding articles from threading entirely), outlet ingestion-status field (#73), authoritative outlet seeder (#93).

## Open strategic questions

Four live unknowns (one — #3 — now resolved below). None block current work; all shape decisions in the next 1–3 months.

### 1. When does sift-api need to scale beyond Railway hobby tier?

Pipeline runs every 30 min, ingests **59 RSS feeds** (`len(services.rss.FEEDS)`; corrected 2026-07-27 from "~135 sources", which was wrong and had propagated into sift/docs/OPERATING_CONTEXT.md — see #104's README fix + drift guard). **Feeds are no longer 1:1 with outlets: 59 feeds = 56 outlets**, because Washington Post now takes four section feeds (#122). Quote the outlet number, not the feed number, in anything user-facing. Calls Claude for summaries + entity linking + primer generation. Today: comfortably under hobby-tier limits.

**What adding sources would cost is now measured** — [`docs/SOURCE_SCALING.md`](./docs/SOURCE_SCALING.md), 2026-08-11, at the post-narrowing rate of $2.69/1k: **+50 outlets ≈ $173–224/mo, +100 ≈ $212–324/mo, +1000 ≈ $1,000–2,971/mo** *and* 227% of the threading queue. Roster narrowing paid for roughly the first 50 outlets — +50 now costs less than the status quo did before it landed. The doc also names the two changes that should precede any expansion (make the entity linker cheaper — by roster narrowing, **not** batching, which was tried and rejected on recall; and rank on distinct outlets) and the one ceiling that is probably binding today (`MAX_ENTRIES_PER_FEED`).

Watch for:
- Pipeline run time approaches the 30-min cadence (e.g. exceeds ~25 min)
- ~~Anthropic monthly bill from pipeline crosses $50/mo (today: ~$15)~~ — ~~**corrected 2026-07-30: actual is ~$10/day (~$300/mo)**~~ — ~~**broken down 2026-08-05 from five full days of `ai_usage_daily` (07-31..08-04): $8.99/day**~~ — **reduced to ~$3.24 per 1k articles as of 2026-08-10 (−39.8%), see the table below.** The original breakdown, kept because every later figure is measured against it: **$8.99/day.** `entity_linker_llm.link_text` **46.2%** ($4.15), `story_synthesizer.synthesize` 26.3% ($2.37), `story_clusterer.cluster` 17.1% ($1.54), `summarizer.batch` 10.3% ($0.93), Voyage 0.02%. Threading (clusterer + synthesizer) is **43.4%**, close to the 39% guessed at `workflows/pipeline_workflow.py:17`.

  **The ledger under-reports.** The three Batch API paths never call `log_usage` — `process_*_batch_results` in `context_generator.py` / `primer_generator.py` / `entity_extractor.py` record nothing — so ~$1/day sits unattributed between this figure and the ~$10/day on the bill.

  **Root cause is a volume assumption, not pricing.** `services/entity_linker_llm.py:32-34` documents its economics as "~100 new articles/day → ~$3-5/month". Actual ingest is **~2,000/day**, so that one call site is ~$125/mo.

  **Verified 2026-08-10, re-run on the full clean day 2026-08-11, and the volume-free column is the one to quote.** Per 1k articles, comparable scope: **$5.38 → $3.22 (−40.1%)**. The directional 2.4-hour figure was $3.24; the full day confirms it.

  | operation | baseline $/1k | now $/1k | |
  |---|---:|---:|---|
  | `entity_linker_llm.link_text` | 2.48 | **1.50** | −39.6% (#130 regex pre-gate) |
  | `story_synthesizer.synthesize` | 1.42 | **0.54** | −61.8% (#129 reuse skip, then synthesize-on-change) |
  | `story_clusterer.cluster` | 0.92 | **0.00** | **−100%** — retired by incremental threading |
  | `story_confirmer.confirm` | — | **0.16** | its replacement, one batched call per run |
  | `summarizer.batch` | 0.56 | 0.65 | see the light-day caveat below; 7-day value is **0.57** |
  | **threading subtotal** | **2.34** | **0.70** | **−70.0%** |
  | **total (comparable)** | **5.38** | **3.22** | **−40.1%** |
  | **total (all recorded)** | — | **3.88** | includes the three Batch API paths |

  **The batch-scheduled stages mis-weight on a light day, and 08-11 was light** (1,090 articles vs a 1,750 run rate). `summarizer.batch`, `primer_generator.batch`, `context_generator.batch` and `entity_extractor.batch` fire on a schedule rather than strictly per article, so their $/1k rises when volume falls — which is why summarizer reads +17% here and −2% over 7 days. **Quote the clean day for threading and the 7-day window for the batch paths.** The threading numbers are the ones the clean day exists to isolate, and they are unaffected.

  **Read `$/1k articles`, not `$/day`.** The `raw` column showed −95.9% on this window purely because it was 2.4 hours long, and has separately shown a −2.4% operation as +17.8% on a busy day. Re-baseline from `scripts/verify_cost_baseline.py`, never by hand.

  **Confidence, stated honestly — and the full-day re-run is now done.** The deploy-check verdicts were structural and solid from the start: `clusterer calls per day 0.00 PASS — legacy path retired`, `synthesize per story touched 1.16 PASS`. The original *dollar* figures came from 2.4 overnight hours and 115 articles and were flagged as directional; **the full UTC day 2026-08-11 has now been measured and they held** ($3.24 → $3.22 comparable). The remaining caveat is the batch-path light-day weighting noted above, not the threading result.
- Neon Postgres connection pool `max=5` starts queuing requests visibly
- Native app launches and pushes write volume up

**Neon storage is a separate bill and was never costed until 2026-08-05: 2,272 MB**, of which `idx_articles_embedding` alone is **879 MB (39%)**. **228,689 of 282,943 articles (80.8%) are past the feed's own 30-day recency floor** ([sift#172](https://github.com/kristenmartino/sift/pull/172)) and cannot be displayed. Retention would take the DB to ~500 MB — design in [`docs/NEON_RETENTION.md`](./docs/NEON_RETENTION.md). Destructive, so archive-before-delete with explicit sign-off; and **do not drop the embedding index** — though ~~which Next 3 #3 makes load-bearing~~ **that reason is wrong and was corrected 2026-08-17**: threading never touches it (`EXPLAIN` plans an exact sort over the 48h × category slice; `idx_scan` is stuck at 75). It is kept for whole-corpus topic search in `sift/lib/db.ts` alone. See `NEON_RETENTION.md` §2.

> **Storage was the wrong half of this bill (2026-08-14).** Storage bills at $0.35/GB-month: 2.11 GB is **$0.38/month**. Every remaining action in `NEON_RETENTION.md` is real work worth well under a dollar a month. Do not spend a day on it expecting money back.
>
> **The bill is compute, and it is billed from the first hour** — Launch has no included-CU-hour allowance, so savings are linear and every avoided wake is money. Measured from the billing page, Aug 1–14: **312.8 CU-h → $33.08** (~$0.106/CU-hour) org-wide.
>
> **The CU-hours are shared across every project in the org, and `sift` is not the largest.** Same 14 days:
>
> | Project | CU-h | ~$/mo projected |
> |---|---:|---:|
> | cratedigger | ~161 | ~$37 |
> | **sift** | **139.3** | **~$32** |
> | tenancy | 12.2 | ~$3 |
> | regrag | ~0 | $0 |
>
> `tenancy` is the control case: same plan, compute **Idle**, $3/month. That is what `sift` should look like.
>
> **Cause, for sift:** `pg_postmaster_start_time()` reported **26 days of unbroken uptime** — the compute never scaled to zero. One 60-second timer did it: the batch poller opened each iteration with a `SELECT` on `api_batches` before checking whether anything was pending, plus `/health`'s two queries every 30 min from the GitHub heartbeat. Both now answer from memory and the poller blocks on an event. See `sift/docs/DECISIONS.md` D54.
>
> **Console settings needed no change and were verified 2026-08-14:** autoscale already `.25 ↔ 2 CU`, **scale-to-zero already ON** (5 min), history retention 6 hours, one branch and one endpoint. That is what makes the diagnosis conclusive rather than plausible — with suspension enabled, 26 days of unbroken uptime can only mean a query arrived inside every 5-minute window.
>
> **Expected after deploy:** sift's 10.0 CU-h/day is **6.0 of always-on floor** (0.25 CU × 24h) plus 4.0 of real pipeline work. Suspending between runs should land ~5.4 CU-h/day → **~$32/mo → ~$17/mo**. Confirm at +48h; do not assume.
>
> **The next lever, if wanted:** with a ~120s run and a fixed 300s suspend tail, **~71% of post-fix awake time is tail, not work** — so wake *count* dominates. `REFRESH_INTERVAL` 30 → 60 min would save ~$4-5/mo at the cost of feed freshness. The run-duration logging added to `_scheduled_refresh` is what makes that measurable rather than guessed.
>
> **Re-derive, don't quote:** `scripts/verify_neon_idle.py --probe` (uptime + size, no API key) and `--api` (consumption history, needs `NEON_API_KEY`). Note it measures ONE project; the invoice covers all four.

### 2. Is the LLM-based entity linker durable, or does it need a v2?

Phase 3.G.2 shipped the LLM linker with disambiguation rules added since. It's working but it's a moving target — every dossier expansion changes its catalog, and the prompt keeps needing tweaks.

What would resolve this: a stable eval set with target precision/recall numbers, run on every PR that touches `services/entity_linker_llm.py`. Until that exists, the linker stays in "iterate fast" mode.

**Update 2026-07-30:** the machinery for that eval now exists and is reusable — `scripts/eval_clustering.py` established the pattern (labeled corpus → deterministic metrics → committed response fixtures with a `prompt_sha256` tripwire → free CI replay + a manual `--live` mode). A linker eval is now mostly corpus-labeling work rather than harness-building. Still the honest blocker: no labeled data.

Separately, the linker was the **largest single cost lever** in the repo — one realtime call *per article*, 46.2% of Anthropic spend. **Addressed 2026-08-05 by #130**, which gates the LLM behind the free regex matcher and forwards only ~26% of articles.

**Batching to ~10 articles/call is superseded, not merely deferred.** That modeled −60% against the *ungated* volume; gating already removes ~74% of the calls, so batching the remainder is a much smaller lever than the old number implies. Do not quote −60%.

**And as of 2026-08-11 it is not just smaller — it is off the table on quality.** Built and A/B'd against the live path: batching gives up **15–18 points of recall** (79.5% at 10/call, 83.6% at 5) against a single-article path that agrees with *itself* 97.3% across two runs. Not a batch-size effect (a batch of **two** loses ~20 points) and not a position effect (no gradient across slots). Reverted, not shipped. Full experiment in [`docs/SOURCE_SCALING.md`](./docs/SOURCE_SCALING.md#batching-the-linker-does-not-work).

**Roster narrowing shipped instead, and it is the better idea anyway** (`entity_linker_roster_narrowing_enabled`, default on). The regex gate already computes which surface forms matched and then discarded them; now it sends the LLM those candidates plus same-surname siblings rather than all 856 rows. Measured end to end through `link_articles` against prod: **2.1 roster rows per call, ~7,300 input tokens down to ~650, $0.0042 → $0.00086 per call (−80%)**. Linker **$1.50 → $0.31 per 1k articles**; all-in **$3.88 → $2.69 (−31%)**, i.e. **~$204/mo → ~$141/mo**.

**And it is more accurate, not a trade** — which the obvious measurement gets backwards. Diffing against the full-roster path reads "94.2% recall, 85.3% precision" and looks like a downgrade; that only means 15% of its links are ones the incumbent missed. `scripts/eval_linker_roster.py` adjudicates disagreements *and* a sampled agreed base blind with Sonnet, over 400 gated articles: **overall precision 82.9% vs 80.9%, and 257 correct links vs 241**. Of links found only by the narrowed path **70.5%** were judged correct, against **46.9%** for those found only by the full roster. Fewer distractors, better answers. (The eval's own first ship bar was wrong — it scored only disagreements and ignored that narrowing produces far more of them; the docstring keeps that trap visible.)

The durability question itself is unchanged and still open — and #130 sharpens it, because the gate's recall now depends on catalog *coverage*: an entity the regex cannot name is one the LLM never gets asked about. `scripts/eval_linker_gate.py` measures that coverage (**97.02% as of 2026-08-06**, from 98.2% on the same 7-day window before `stat` was blocked; it read 97.63% against the ten-name blocklist on 2026-08-05, over a different window) and should run on every PR touching the linker or `entity_aliases`.

**Do not read those three numbers as a trend — since the full-corpus backfill they are not on a common basis.** The metric scores the regex against *stored* links, and `backfill_entity_links.py --include-empty` (#141) wrote the **regex's own output** into `entity_links` for 54,240 articles, so it now partly scores that backfill against itself. Re-running on merged main gave **98.83%**, higher than every figure above and not because the gate improved: the post-alias-seed split read **99.91% (n=1,140)**, against 97.63% pooled and n=6 pre-backfill. Near-perfect is the tell. The pre-backfill reads were clean and stand as history; they are simply no longer reproducible by this script, and no post-backfill number can confirm or refute them. Recovering a common basis needs provenance on `entity_links` (which path wrote each one) or an LLM-only re-link of a held-out sample — a real change, not a doc fix. Until then quote pass-through and the miss buckets, which are unaffected.

**Read that recall number knowing what it scores.** It scores the gate against *stored* links, so blocking a name that is producing bad links shows up as a recall *loss*. The whole 1.18-point drop above is 33 articles, and all 33 are links that should not exist: 26 are STAT News's own articles losing a self-reference chip and 7 are the sports-copy false positives. A blocklist addition will always look like it costs recall on this metric; check what the newly-missed articles actually are before believing it.

### 3. Does `sift-mcp` merge into `sift-api`?

**Resolved (2026-05-20) → merge.** Canonical record: [`sift/docs/DECISIONS.md` D41](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md); tracked at #62 (Phase 0 pending). Fuller discussion archived in [`docs/STATUS_ARCHIVE.md`](docs/STATUS_ARCHIVE.md).

### 4. DMCA fair-use posture for AI summarization

Per Railway's 2026 fair-use clause (lists "Hosting/Distribution of DMCA protected content" as prohibited) + the live NYT / Perplexity / AP litigation landscape: audit needed to confirm `services/` doesn't write original article body HTML or images to disk on Railway, and `/methodology` (sibling `sift` repo) needs a transformative-use posture paragraph before any user-submitted-URL features (e.g. iOS share extension) ship. Tracked as [#54](https://github.com/kristenmartino/sift-api/issues/54).

## Next 3 — moved to GitHub

**This section is gone deliberately.** This repo's own history has an instance of the failure mode this is meant to prevent: the Next 3 numbering itself drifted out of sync with reality — item 2 sat marked "done · verified" instead of being retired, and the list's own footnote had to explain in prose that it was "four while item 2 is awaiting verification... collapse it back to three once that lands." A hand-maintained priority list needs a human to notice and edit it every time an item's status changes; a GitHub issue's state changes when the issue closes.

    gh issue list --state open            # the engineering queue
    gh pr list --state open               # in flight

Priority lives in issue labels and effort tags (`tier-*`, `effort-*`). **Non-engineering next actions — anything
CLAUDE.md's "where to file new work" table says never becomes a GitHub issue — live in Active focus above, not
here.** This file is a decision log, not a queue.

## Blocked-on — moved to GitHub

**Also gone deliberately.**

    gh issue list --state open --label blocked

### What this file is for

STATUS.md holds no current state of its own. Current state lives in GitHub issues/PRs, and in Active focus above
(bounded, current, rewritten not appended). What has no home in GitHub is the cross-issue record — we measured X,
it refuted Y, here is why we did not do Z — and architecture-level decisions promote further into
[`sift/docs/DECISIONS.md`](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md) in the sibling repo.
An entry below describes what was true on its date and is never edited to stay current.

## Recent decisions (last 7 days)

**Entries before 2026-08-13 are archived** in [`docs/STATUS_ARCHIVE.md`](docs/STATUS_ARCHIVE.md). This section
held 58 entries going back to ~2026-05 (44 archived, 14 kept below).

- **2026-08-18** — **The instrument that was supposed to validate the judge had not been validated itself, and the judge turns out to be blind to the failure mode that matters** (#243). `eval_judge_calibration.py` decided what to plant by regex over the SUMMARY and never read the article — the parameter was literally named `_article`. So "RTVE allegedly skipped introducing an athlete" -> "definitely skipped" was planted as a `legal_safe` violation. No charge, no court, no investigation; the rubric's own "true if no legal matter is involved" makes `true` correct, and **a right answer was recorded as a missed detection.** That single invalid case is the whole distance between the reported "1/2, below the bar" and 1/1.

  **This is the retraction's defect one level down.** The retracted 0.288 came from a judge nobody had validated; its replacement came from a VALIDATOR nobody had validated. #245's conclusion that the corpus was too clean was also wrong — the predicate required an active-voice hedged verb, so "is under investigation for" matched and "is investigating" did not. Widening it, plus pooling a second 450-article capture, took plantable summaries from 2 to 21.

  **The finding, once the ground truth held:** the judge is perfect on escalation (**12/12, unanimous**) and scores **0/5 on a deleted hedge**, also unanimous, and 0/6 under all three readings of the axis. Grading the loud tier alone would have read 100% and certified the instrument — **graded severity is the only reason this surfaced**, so detection on `loud` is now reported as a floor, never a rate.

  **A narrower question mostly fixes it**: one yes/no per claim against the article's own sentence reaches 3/5 with 0/38 false positives, where the three-axis rubric got 0/5. Same model. The rubric was asking at the wrong altitude, not failing at the task. Two approaches were tried and rejected on the way and are recorded in the module docstring so they are not retried: a pure string rule (2/5, and 7 false positives including a correctly-hedged summary), and omitting the article title — **#245's own finding, reintroduced**, which flagged the Mangione summary for "admitted to shooting" against a headline that reads "admits killing him".

  **What this changes about how evals get read here.** A pass rate on an axis nobody has shown the judge can fail is not evidence. `tests/test_eval_judge_calibration.py` now pins the three properties that make a plant valid ground truth, verified by mutation — and the first draft of one of those tests SURVIVED disabling the guard it tested, because it asserted on the start of a string while the defect sat mid-sentence. Same can-it-fail defect, one level up again. Sibling: `sift/__tests__/meta.test.ts` and `sift/stryker.conf.json`, same question of the frontend suite.

- **2026-08-18** — **CI now gates a merge in both repos, and both gate on their linter.** The finding recorded a day earlier is closed.

  `required_status_checks` rules are active on both `main` branches — **`Type Check & Test`** in `sift`, **`Lint & Test`** in `sift-api` — alongside `pull_request` and `non_fast_forward`. `sift`'s existing ruleset was extended rather than replaced; `sift-api` had **no protection of any kind** and now mirrors it, which means direct pushes to `main` no longer land there. `required_approving_review_count` stays at 0 (a solo maintainer requiring an approval blocks only themselves), and `strict` is off, so a moving `main` does not force a rebase on every PR. No bypass actors: a gate with a hole in it is the state this replaced.

  **It proved itself on the way in.** `sift-api`#262 went `BLOCKED` on push and only became `CLEAN` once `Lint & Test` reported — the same PR that, a day earlier, would have been mergeable red.

  **`sift` gained a `Lint` step** ([#273](https://github.com/kristenmartino/sift/pull/273)), which it had never had: `ci.yml` ran audit, `tsc`, jest and the production build, so the three ESLint errors sitting on `main` were invisible to CI and only ever surfaced by hand. Those are fixed in the same PR — two `react-hooks/set-state-in-effect` (`CoachStrip`, `NewsAggregator`) and an `<a>` to an internal route. `CoachStrip` now reads localStorage through `useSyncExternalStore` like `useTheme` does, and gained the test file its behaviour never had. `sift-api` already ran `ruff check .` inside its required job — verified in the Actions step log, not assumed.

  **The gate starts green in both repos, deliberately.** `eslint .` exits 0, `ruff check .` passes, 886 jest tests, 894 pytest. Switching a required check on over a known-red tree teaches people to bypass it.

- **2026-08-17** — **The suite was audited for whether it can fail. `test_meta_suite.py` caught the easy class; these got past it.** **(The "CI gates nothing" finding at the end of this entry was closed 2026-08-18 — see above.)** Every finding was proved by mutation before the test was touched: break the source, confirm the suite stays green, rewrite, confirm it now goes red.

  **Story identity was guarded by a tautology.** `test_a_later_joiner_does_not_change_it` called `seed_story_id` twice with the **same** argument list — `f(x) == f(x)` — under a comment reading `# 'c' joins later`. Nothing joined. That is the property `workflows/incremental_threading.py` exists for: 58,259 of 58,557 story rows were orphaned by ids derived from current membership. Appending `str(len(member_ids))` to the hash key, restoring that exact bug, left the file green. It now creates a story and grows it through `_attach` for real. **The expected id is a golden literal**, because the first rewrite recomputed it with `seed_story_id` and *also* survived the mutation — under a mutated hash the fixture and the assertion move together. The trap is the same one, one level up.

  **Retries were unbounded in effect.** `MAX_SYNTHESIS_ATTEMPTS` and `SWEEP_LIMIT` were asserted against the same module constants `_sweep_failed` binds as query parameters, so the test pinned argument *position*. At 10,000 and 5,000 it stayed green while every structurally-failing story would re-synthesize every 30 minutes forever at $0.0022 a call — which is what the test's own name says it prevents.

  Five smaller ones, same method: `latency_ms` was asserted on a dataclass the test itself constructed, so hardcoding `0.0` sent every latency figure in the system to zero with the suite green; `CAPABILITIES` was checked for non-emptiness, so `compare.search_sources` could declare `BATCH` and silently lose the server-side web search it is built around; `cost_guard` patched `get_pool` itself, so the ledger-write swallow path it claimed to cover was never reached; `assert "Whatever" not in str(index)` in `test_scrape_committees` referenced a string that appears nowhere in the repo, true for every possible implementation; and the fallback-sources lowercase invariant was checked on one element.

  **The meta-suite now catches vacuity, not just absence.** It rejected assertion-free bodies but not `assert f(x) == f(x)` — and neither does Ruff (`PT015`/`B011` only fire on `assert <literal>`). Added that check, plus a structural check for `test_*` defined where pytest will not collect it (a method in a class not named `Test*` is counted by the guard and never runs). Its own guard-the-guard was ratcheted from `> 100` / `> 10` against a real **772 functions across 50 modules** — it tolerated losing 87% of the suite before firing.

  **Coverage floor 55 → 74** against a measured 75.89%. It had ~21 points of slack, so a fifth of the coverage could have been deleted with the build green — the same defect `sift`'s jest config candidly documents about its own threshold. The "known zeros worth attacking first" comment named three modules now at 58%, 95% and 93%; replaced with measured figures.

  **The finding that is not in a test file: CI gates nothing.** No `required_status_checks` rule exists in either repo, and this one has no branch protection at all, so merges land as direct pushes to `main`. `ci.yml`'s comments were written for branch-protection semantics and the path-filtered jobs already post green when skipped — the groundwork is done, the rule was never added. Left for the owner; it is a settings change, not a code change.
- **2026-08-17** — **`world` was absorbing US-domestic tabloid content by elimination, not because "world" reads as "not-US-politics" and not because of a feed's general/dedicated shape** ([#227](https://github.com/kristenmartino/sift-api/issues/227)). Neither hypothesis in the issue survived contact with prod data as originally framed. Sampled 25 `world`-tagged articles per source (7d window, n=819) and LLM-judged each us-domestic vs. international: **general-firehose sources averaged 8.9% misfiled, indistinguishable from BBC World's dedicated-feed 8.0%** — feed shape does not discriminate. New York Post alone sat at **44%**, filing genuine NJ/LA/Long Beach/Times Square crime and accident stories into `world` alongside real Iran/Israel coverage. The driver is content mix, not source or feed shape: NY Post's tabloid volume of single-incident US crime/accident/human-interest stories with no topical fit is unusually high, and the ten-category prompt had no honest landing spot for that content — "world" and "politics" both absorbed it by elimination (the live before/after batch below also caught Penn State cocaine-bust and 911-murder stories wrongly filed under `politics`, the same root cause).

  **Fix: exposed the existing `FALLBACK_CATEGORY = "general"` sink as a deliberate 11th prompt option** (`services/summarizer.py`) instead of widening the taxonomy — it already had the right semantics ("stored and searchable, never ranked... misfiling by policy is worse than not filing"), it just wasn't offered to the model, so `"general"` was oddly absent from `VALID_CATEGORIES` even though it was the fallback value. Tightened `"world"` to require the event itself be located outside the US, and routed the "if none fit" instruction through `general` instead of implicitly allowing `world`/`top`.

  **Measured before/after on the same live-fetched batch** (79 articles across the 8 sampled sources, `scripts/eval_world_misfiles.py --mode compare`, no DB writes — `raw_content` isn't persisted, so a stored-row replay would be confounded by the news having moved on, same limitation noted for `compare_content_change.py`): `world` **14 → 8** (6 moved out, all genuinely non-international — an NJ car crash, a Hawaii mudslide, two Iran-war pieces recategorized to `top` as the actual major-significance call). `general` **0 → 12** total, absorbing not just the `world` misfiles but pre-existing `politics`/`business`/`entertainment` misfiles of the same shape (a murder trial, a fraternity drug bust, a gambling poll — none of them political). Genuine international content (Iran/Israel, an orca investigation, FT world pieces) stayed on `world` in both passes — no regression found on a manual spot check of the 8 that stayed.

  **sift-api#226's proposed low-importance dampener for `world`** may no longer be needed now that the tab's content mix should be substantially cleaner — worth re-checking `world` ranking after this deploys, rather than proceeding with #226 as originally scoped.

- **2026-08-17** — **DIME gives us the campaign-finance question OpenSecrets is blocking, just not the same one** ([#260](https://github.com/kristenmartino/sift-api/issues/260); read side is [`sift`#264](https://github.com/kristenmartino/sift/issues/264)).

  Stanford's DIME was ruled out as an industry source and that stands — no `catcode`/`realcode`/industry field in any tier, and the CRP-derived records it does gate are **state-level**, so academic access would not supply federal catcodes even if granted. But `dime_recipients_1979_2024.csv.gz` carries FEC-reported **composition** per candidate per cycle: total receipts, itemised vs unitemised individual, PAC, party, self-funding, `ind.exp.support` / `ind.exp.oppose`, and `num.givers`.

  **Checked before proposing**, since this file has twice been wrong by quoting a figure that had moved: Warnock 2022 = **$206,593,948**, Ossoff 2020 = **$156,146,538** — both match the known public totals. **524 of 537** current members join on `Cand.ID` → `congress-legislators` FEC id → bioguide; **ICPSR is only 319/537 and is the wrong key**. 100% fill on every money field for 2020, 2022 and 2024. ODC-BY 1.0 — commercial use permitted with attribution, which the OpenSecrets lane does not allow.

  **Two things the import must carry, or the read side cannot be correct.** Store the **cycle** beside the figures — the lesson of `sift`#251, where the year lives only in hardcoded copy. And store enough to identify a member's most recent *election* cycle rather than their latest funded row: Warnock's 2024 row is $4.8M of off-cycle committee activity against $206.6M in 2022, and `gwinner`/`gen.vote.pct` are **0% populated for 2024**, so outcomes cannot do that work.

  Carry DIME's own caveats too — entity resolution is probabilistic ("encouraged to hand-check the IDs") and duplicate records are known — and spot-check the joined rows before publishing, the discipline `verify_role_sources.py` set.

  **Not taken:** the CFscores. `composite.score` is multiple over-imputation plus PCA, a modelled composite of exactly the kind `sift` D37 refuses to derive or characterise.

- **2026-08-17** — **Migrations 031–033: the write-path half of `/term/<slug>`.** 031 adds `term_profiles` (hand-sourced definitions), 032 a full-text GIN index over `title || summary`, 033 an IMMUTABLE `primer_term_keys(jsonb)` plus a GIN index on it. Read path and the reasoning live in [`sift/STATUS.md`](https://github.com/kristenmartino/sift/blob/main/STATUS.md) and `sift/docs/DECISIONS.md` D55–D58; what belongs here is the schema and the two measurements that shaped it.

  **032 — the index does not change what the page claims.** "Which articles mention this term" was a parallel seq scan of all 305k rows: 1,000–1,800 ms and ~52,000 buffers per term. Postgres FTS *stems*, so `phraseto_tsquery('english','temporary protected status')` is really `temporari <-> protect <-> status` — looser than the phrase. The read query therefore uses the index as a **prefilter only** and keeps an exact word-boundary regex as the confirming predicate. Verified lossless: regex-only and prefilter+regex returned identical counts (133 / 66 / 9) on every term with volume. 34–81 ms after.

  **033 — a function, not `@>` containment.** `context_primer->'terms' @> '[{"term":"…"}]'` is GIN-indexable but case-**sensitive**, and the generator is inconsistent (`redistricting` 483, `Redistricting` 2). Folding case inside an IMMUTABLE function keeps the match case-insensitive *and* indexed: 0.2 ms, 82 heap blocks, 2.6 MB over the ~97,500 articles carrying a primer terms array (measured 2026-08-17; the corpus grows daily, so treat every count here as a reading, not a constant). `STRICT` plus `jsonb_typeof` guards make it total — a malformed primer yields `[]` rather than an error, so a bad row cannot break the read path.

  Seeders: `seed_term_profiles.py` (five validations, each mutation-tested — every guard confirmed to fail when removed before being trusted to pass) and 24 curated rows in `data/term_profiles.csv`. **Every citation was refetched and asserted to name the term** before a definition was written, per `verify_role_sources.py`; two of 22 candidates 404'd and were dropped rather than guessed at.

- **2026-08-17** — **`source_name_aliases` had 0 rows in prod, since the table was created.** Invisible because the read queries fall back to matching `outlet_profiles.name`, so the 43 outlets whose feed name equals their dossier name resolved anyway and nothing reported the other **41.8% of articles**. `data/source_name_aliases.csv` adds the 15 rows that change behaviour; the exact-name matches the audit also proposes are omitted as no-ops. Resolution 58.2% → 70.5%.

  **The misses were not random**, which is what made it a D37 matter rather than housekeeping: outlets filing under variant names (The Guardian US, New York Times, Washington Post) skew left, so `/term/*`'s spectrum block under-reported left coverage 21 vs 51 while right, whose outlets file under canonical names, was unchanged. Also restored recent-story lists on six outlet dossiers that had been rendering the empty state, and lean badges across the feed.

  Rows are hand-checked one at a time. The audit's `substring` tier is a review heuristic and **not data** — it proposed `IGN → foreign-affairs`, matching "ign" inside "foreign". A later sweep for unresolved names sharing a token with a *rated* dossier surfaced five more candidates and only one was real (`BBC World`); `Times of India`/`Japan Times` → *The Times* and `ABC Australia` → *ABC News* are different publications entirely.

- **2026-08-17** — **Two of the three remaining `NEON_RETENTION.md` actions were resting on unmeasured arithmetic, and both are now withdrawn.** §3 promised `VACUUM (FULL, ANALYZE) api_batches` would reclaim "~40 MB of dead tuples." That 40 MB was never measured — it was `pg_total_relation_size` minus heap minus indexes, and the remainder is the **TOAST table**, holding the live `metadata` jsonb of 14,933 batches: **`n_dead_tup` = 0**, and `sum(pg_column_size(metadata))` = 46.9 MB against a 46.6 MB TOAST. An `ACCESS EXCLUSIVE` lock on prod to return approximately nothing. Not run. And §2's "do not drop the embedding index, threading depends on it" was stale from the day it was written — `INCREMENTAL_THREADING.md` had already established the opposite; `idx_scan` is still **75**, unmoved since 2026-08-05.

  **The rule worth keeping: `total − heap − indexes` is TOAST *plus* bloat, not bloat.** Both errors are the same error — a size subtraction believed without reading `n_dead_tup`. It is also the third time this document has over-promised a reclaim, after VACUUM-does-nothing (twice) and the 106 kB below.

  **Retention itself, re-priced today: $0.74/mo → $0.15/mo.** The database is 2,118 MB, `articles` 96.6% of it, 250,415 of 305,106 articles (82.1%) past the 30-day floor. Deleting all of them is a destructive day plus a 2 GB rewrite for **59 cents a month**. The verdict changes only if the corpus grows an order of magnitude or the feed gets real traffic.

- **2026-08-17** — **The orphan-`stories` cleanup is closed: 60,646 rows deleted across five passes, 0 remaining, 0 dangling references.** The last 387 came out today, once they aged past the script's 48-hour `updated_at` floor — all pre-cutover, newest `updated_at` 2026-08-10 17:38Z, one minute before the legacy path stopped writing. **The marginal rate never moved off zero:** 0 orphans among 354 post-cutover stories on 08-11, 0 among **1,251** today. Incremental threading's seed-derived `story_id` (#158/#161/#180) is the reason, and it is now confirmed over nine hundred more stories than the last check.

  **The reclaimed space is 106 kB and that is the correct result** — `stories` is under 4 MB in total, autovacuum had already zeroed `n_dead_tup`, and `VACUUM FULL` was deliberately skipped rather than take an `ACCESS EXCLUSIVE` lock for nothing. Database size is `articles`, not this; see `docs/NEON_RETENTION.md` §1. **The "8 unrepairable" figure quoted below is now 1** — the 7 orphans are gone and only single-member `1b72a6941cfd8e75` remains, owned by no tool and invisible in the feed by design. 48h grouping rate 34.1%.

- **2026-08-13** — **Sift was discarding real article text on a quarter of its feed, and now reads it** (#240, verified by #242). `parse_feed` read `summary`, then `description`, then `content` — first match wins, and `summary` is present on nearly every entry, so `content:encoded` was almost never reached. Measured across all 59 feeds: **26% of entries carry a content field more than twice as long as the one being read.** ProPublica: 20 words read against **3,186** available, and the 20 were the WordPress footer, "The post … appeared first on ProPublica."

  **Not a scraping change** — same request, same publisher-supplied feed, no paywall, no robots.txt question. It does not touch Open-Q #4's fair-use posture at all. It matters well beyond the summarizer, because category, importance, tone, genre, why-it-matters, primer, entities, clustering and synthesis are **all** derived from the summary this text produces. The pipeline's entire view of an article was a 25-word teaser.

  **Deliberately not "longest wins."** Inspection showed that would be wrong: The Atlantic's photo posts carry ~900 words of caption and photographer credits, Ars Technica's opens with nav furniture, Politico ships a 68-word teaser and an *empty* content element. Content displaces the teaser only at ≥100 words **and** ≥3× its length, plus a rule for boilerplate teasers. Effect over 579 entries: articles with ≥100 words **21 → 163**, mean body words **42 → 327**. Cost **+$0.129/1k** on summarizer input, ~$7.50/mo — against ~$22/mo the DeepSeek swap would save on the same stage, so the two are complementary.

  **Verified by a tool built for it** (`scripts/compare_content_change.py`), because the obvious check does not work: `articles` never persists `raw_content` and post-deploy rows are *different articles*, so a stored-summary diff is confounded with the news changing. It reconstructs both inputs for the same entry and varies only the input. **20 of 20 improved**, generic → specific throughout.

  **Two consequences that are not free wins.** Classification is downstream of the summary, so **the feed's category mix will shift** — an electric-aircraft story moved `technology` → `energy` once the model could see fuel-price context. And **legal exposure rose**: concrete claims about named individuals are now far more frequent while the prompt rules governing them are unchanged. That is Next-3 #0 / #243.

- **2026-08-13** — **The model-swap question is answered on cost and open on quality.** Haiku 4.5 is the cheapest Claude, so this was always a leave-Anthropic decision. Measured on real tokens against primary-source vendor pricing: **DeepSeek V4 Flash ~$99/mo cheaper pipeline-wide** (5.1× under the incumbent), against gpt-5-nano's $71/mo — and DeepSeek is also closer to the incumbent on categories (0.937 against a 0.924 self-agreement floor). Kimi is *dearer* than Haiku on this pipeline's token shape: K2.6 by 3%, K3 by 292%.

  **The finding worth carrying is structural, not financial.** At Haiku's own `max_tokens=700`, both candidates produced **nothing** — 30/30 empty batches for gpt-5-nano, 100% of output spent reasoning — with zero provider errors. It arrives as an empty string failing `index_alignment`, which degrades to `_raw_content_fallback` writing truncated RSS text as a summary while the run reports success. **Reachable today by setting one env var.** Every `max_tokens` in the repo encodes an assumption that the model does not think before answering.

  Quality remains unmeasured on prose. A faithfulness eval was built, run, and **retracted**: it reported the incumbent at 0.288 "supported", which was measuring RSS stubs rather than the model. `scripts/eval_summary_quality.py` now refuses below 40 articles at 100+ body words rather than emitting that number again.

- **2026-08-14** — **The batch poller was billing Neon 24/7 to ask a question this process already knew the answer to.** `pg_postmaster_start_time()` reported **26 days of unbroken uptime**: the compute had never once scaled to zero. `run_batch_poller` looped every 60s unconditionally and `poll_pending_batches` *opened* with a `SELECT` on `api_batches` before checking whether anything was pending — 1,440 queries/day landing inside Neon's 300s scale-to-zero window. `/health` added two more every 30 minutes from the GitHub heartbeat. Both now answer from memory: the in-flight batch set is recorded by `submit_batch` (every submitter runs in this process), `last_pipeline_run` is mirrored by `store_node` (the sole writer of `pipeline_state`), and the poller blocks on an event rather than a clock. Postgres is read once at startup for crash recovery and reconciled once per pipeline run — both already-awake windows. See `sift/docs/DECISIONS.md` D54.

  **The console needed no changes**, and that is the point: scale-to-zero was already enabled and the autoscale floor already 0.25 CU, so 26 days of uptime could only mean a query per window. **The intuitive culprit was wrong** — `asyncpg`'s `min_size` is not a floor (`_minsize` is read only in `Pool._initialize`; nothing refills the pool) and Neon suspends on absence of *queries*, not connections.

  **Also wrong: the pricing model.** Launch has no included-CU-hour allowance — compute bills from the first hour, so savings are linear. And the org's CU-hours are shared across four projects, where this one was not the largest. Expect ~$32/mo → ~$17/mo here; **verify at +48h with `scripts/verify_neon_idle.py`**, which exits non-zero while the compute is still pinned. Two smaller finds shipped alongside: `BATCH_GIVEUP_HOURS` bounds a batch Anthropic loses (the old path retried a failing download every 60s until the process died), and `_scheduled_refresh` now logs its duration — the HTTP path always did, so the cadence that runs 48×/day was the one with no timing data.

  **Rule added to `CLAUDE.md`:** never add a polling loop that queries Postgres on a timer under 300s. A sweep found no others — the remaining loops are the 30-min pipeline and two daily monitors.

- **2026-08-13** — **Clustering has a measured accuracy number for the first time, and recording it broke two things in the harness that recorded it.** `--replay` has been described in its own docstring as "what CI runs" since it was written; it never had, because the fixtures and baseline it replays did not exist. Recording them (`--live --repeats 15`, ~$0.36) and wiring the CI job made the claim true — and immediately falsified two others.

  **The number: ARI 0.538, pairwise F1 0.779, multi-outlet precision 0.607.** Against the retracted "~97% accuracy" in `sift/docs/DECISIONS.md` D26/D27, which should now be updated in the sibling repo. **Caveat that has to travel with it:** the corpus labels have never been human-validated — `review_pairs.csv` holds 40 blind pairs with 0 verdicts, and no annotator provenance is recorded. So this measures agreement with the labels, not with the truth, and it cannot yet adjudicate a cross-vendor comparison: an LLM-labeled corpus scored by an LLM judge measures family agreement. ~20 minutes of labeling closes that.

  **`run_live` averaged 2 of its 9 metrics.** `out = dict(runs[0])` took everything from run 1 and replaced only `ari` and `pairwise_f1` with a mean, so seven metrics were single draws while the output claimed to be a multi-repeat result — including `multi_outlet_precision`, which the regression gate checks and which spreads 0.29 across repeats.

  **The tolerance was 0.05 against measured spreads of 0.11 and 0.23**, so the gate would have failed CI on pure noise the first time it ran honestly. Its own comment said to set it from the observed spread after a `--repeats 5` run; this was that run.

  **Deriving from the raw range is also wrong, and it looks right.** 2× range gives `multi_outlet_precision` an allowance of 0.59 against a value of 0.62 — a gate that can only fire below 0.11. Both the baseline and any future live run are **means**, so what must be cleared is the variability *of the mean*, sd/√n. Tolerances are now derived at 3× SE: **0.053 and 0.074**.

  **n=5 cannot estimate a standard deviation.** The tell was two consecutive 5-run sets whose means differed by 0.082, more than the 0.080 tolerance either produced. At n=15 the sd is 25–60% larger (0.051→0.068, 0.060→0.096) and it explains that drift, which the n=5 estimate did not. Chi-square with 4 df is not a stable variance estimate. **The general lesson for the model-swap harness: clustering at temperature 1.0 is far noisier than the linker's 97.3% self-agreement, so an A/B on it needs many repeats or a large effect to be conclusive.**

  **Replay and live now gate separately** — replay is deterministic and judged against its own recorded draw at 1e-6, because a stochastic-sized allowance there would let a real parser regression through. All three detectors were exercised against known-true cases (replay reproduces to 0.000; a blanked fixture and a stale `prompt_sha256` each exit 1), per the standing rule that an unexercised detector is an untested one.

  **A correction to the plan this came from:** it claimed `data/eval/*.labels.csv` being gitignored "ignores the most expensive, least reproducible artifact in the repo." Wrong — the labels live in the tracked corpus and `--export-labels` regenerates the CSV byte-identically. The ignore rule is deliberate and correct.

- **2026-08-17** — **#240 moved the cost curve, and the summarizer's output ceiling with it.** Reading `content:encoded` instead of the short `summary` field is a quality win that no cost figure in this file reflected. Input tokens per call, 08-13 vs 08-17:

  | operation | before | after | |
  |---|---:|---:|---:|
  | `summarizer.batch` | 1,128 | 1,639 | **+45.2%** |
  | `story_synthesizer.synthesize` | 749 | 848 | +13.1% |
  | `context_generator.batch` | 1,777 | 1,893 | +6.6% |
  | `entity_linker_llm.link_text` | 624 | 659 | +5.6% |
  | `primer_generator.batch` | 1,573 | 1,651 | +4.9% |
  | `entity_extractor.batch` | 1,073 | 1,100 | +2.6% |

  **The summarizer is the only stage that reads raw article text**; everything downstream reads the *summary*, so their few percent is just slightly fuller summaries. Summarizer cost went **$0.62 → $0.72 per 1k articles**. All-in is unchanged at **$2.0–2.4/1k** — the increase is real but small against the total and masked by volume swings, which is exactly why it needed measuring rather than eyeballing.

  **It is not finished growing.** Input per article has gone **71 → 173 tokens** and sits at **27% of the ~650-token cap** `_truncate(content, 500)` imposes; the feeds carry far more (ProPublica publishes 3,186 words where Sift used to read 20). If it saturates that cap, the summarizer reaches **~$1.22/1k**, about **+23% all-in**. Worth having, but it should be a decision rather than a drift — re-read this before quoting any $/1k figure.

  **The output ceiling was the near-term risk, and it is now derived rather than fixed** (`OUTPUT_TOKENS_PER_ARTICLE × BATCH_SIZE + scaffolding`, 700 → 1,320). Measured peak output drifted **481 → 567 of 700 (81%)** over six days while *mean* output moved only 332 → 359 — summaries are length-bounded by the prompt, not by input, so it drifts rather than runs away, but it drifts toward a cliff. **The cliff is sharp and silent**: a response cut off at the cap is truncated JSON, which fails `index_alignment`, which re-asks and then degrades the *whole batch* to `_raw_content_fallback` — five articles served truncated RSS text while the run reports success. `max_tokens` bills on tokens *used*, so headroom is free and the asymmetry is one-sided. **Third instance of the same bug class**: `story_synthesizer`'s fixed 1024 was breaking exactly its biggest clusters, and gpt-5-nano produced 30/30 empty batches at 700. A test pins the ceiling to `BATCH_SIZE` *in the source*, not to its value — replacing the formula with the literal it evaluates to leaves every arithmetic assertion passing.

---

*See also: [`CLAUDE.md`](./CLAUDE.md) (orientation), [`BACKLOG.md`](./BACKLOG.md) (deferred items), [`README.md`](./README.md), [`init.sql`](./init.sql), and [`sift/docs/DECISIONS.md`](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md) — the **canonical cross-repo decision register** (record shared architecture decisions there, not duplicated across STATUS files). Sibling repos: `sift` (frontend, owns user-facing reads + civic surface), `sift-mcp` (MCP server, separate cadence).*
