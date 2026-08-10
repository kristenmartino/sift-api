# Incremental threading

**Status:** designed 2026-08-05. **Half built 2026-08-07** — candidate engine and shadow observation shipped; the write path is not. `effort-week`, deferred by [D46](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md).

| | |
|---|---|
| P1 ivfflat recall | **resolved — does not apply.** The planner uses exact sort for this query shape |
| P2 entities-lag watermark | **solved** — per-row `threaded_at` marker, no watermark |
| P3 ship dark | **done** — flag off by default, shadow runs free every cycle |
| Candidate engine | **done** — `services/story_matcher.py`, 10 tests, validated against prod |
| Write path (LLM confirm → attach / synthesize) | **done** — `services/story_confirmer.py`, `workflows/incremental_threading.py`, 11 tests |
| Cutover | **done 2026-08-10** — bar met over 90 shadow runs, see below |

## Cutover evidence (2026-08-10)

`scripts/shadow_summary.py`, 90 runs with the confirm dry run over 48h:

```
                        articles  grouped    rate
live rescan path           2,668      129   4.8%
incremental (shadow)       3,600    1,024  28.4%

confirmer: 1024/1523 confirmed, 33% rejected   (attach 481, new 543, none 499)
outlet gate: 55% of new-cluster candidates carry >=2 outlets
near misses: 32% of parked, baseline 36% by chance, excess -3%

verdicts:
  grouping parity          PASS   28.4% vs 4.8% live
  confirmer discriminates  PASS   33% rejected
  threshold not too strict PASS   32% vs 36% expected by chance
```

### The one objection the bar does not test

Attach candidates were confirmed at **93%** (713/763) against **53%** for new
clusters (895/1697), and that gap held from the first sample through 140 runs.
A 93% acceptance rate looks like rubber-stamping on the path that mutates
existing stories, and the three cutover verdicts would have passed regardless.
It was investigated separately.

**Not similarity.** Attach candidates average 0.754 cosine against 0.714 for
loose neighbours — a +0.040 difference, far too small to move acceptance forty
points.

**Not position bias.** The prompt always lists attach options first. Re-running
identical candidates with the order reversed changed nothing: 6/6 attached
either way, zero decisions flipped.

**It is a selection effect, and the rate is correct.** An attach candidate only
exists when a neighbour is *already in a story* — and a story only exists
because the confirmer previously agreed that two or more outlets covered one
event. So an attach candidate is a new article matching, at >=0.60, something
the system has already validated as a coherent multi-outlet event. That is a
far stronger prior than "two loose articles look similar", which is what a
new-cluster candidate is, and where same-topic-different-event is common.

Hand-read of 14 attach decisions: 13 attach, 1 none. The attaches were four
separate Colombian earthquake reports joining the earthquake story, two
Zuckerberg-manifesto pieces joining the manifesto story, and a Trump
White-House-counsel announcement joining the appointment story. The single
rejection was *"A Higher Nominal Growth World Brings Higher Volatility, Says
Jim Caron"* against *"A Hot CPI Report Causes Problems for Fed, Caron Says"* —
same topic, same commentator, different segments. Exactly the distinction the
call exists to make.

**Caveat:** 14 hand-read decisions retire the objection; they do not establish
a measured accuracy figure. `STATUS.md` Next-3 #1's labelled corpus is still
what would.

### Watch after cutover

- `stories` rows with zero members should approach 0 (was 99.5%).
- Grouped-article count per category should rise, sports and politics most.
- Ingest → story-attachment latency should stay <= 35 min.
- `threading_shadow` keeps recording; the shadow and live paths now agree by
  construction, so a divergence means something regressed.

**Story identity is the substantive change.** `story_workflow` derives `story_id` from a sha256 of its member ids, so gaining an article makes a story a *different story* — a new row, and the old one orphaned. That plus the blanket `UPDATE articles SET story_id = NULL` is the whole mechanism behind 99.5% orphans.

`seed_story_id(category, seed_member_ids)` derives an id **once, from the members that created the story**, and never again. Attaching writes one `story_id` and leaves the row alone. Deterministic rather than a uuid so a repeated seed is idempotent; derived from the seed rather than current membership so growth cannot change it.

**Synthesis runs only when the outlet set changes.** `framings` is a per-outlet structure, so a story gaining its third piece from an outlet it already carries has nothing new to synthesize — the same duplicate spend #129 removed, in its incremental form.

Validated against prod, 40-article simulated run: 4.5s of Postgres, $0, **18 of 40 needed an LLM opinion and 22 parked for free.** It found four outlets on the same jobs report at 0.81–0.89 similarity — one already in a story, three loose that the live path had missed.
**Supersedes:** "raise `LIMIT 50`" (`STATUS.md` Next 3 #3, prior framing).
**Companion:** [NEON_RETENTION.md](./NEON_RETENTION.md) — the other half of the structural work, independent of this.

---

## The problem is the shape, not the parameters

Threading is **43.4% of Anthropic spend** ($3.90/day of $8.99, `ai_usage_daily` 2026-07-31..08-04) and produces a **0.9% grouping rate** on sports. Three measurements explain why, and none of them is fixed by tuning a constant.

**1. The window is not 48 hours.** `workflows/story_workflow.py:50-54` selects `published_date > NOW() - 48h ... ORDER BY published_date DESC LIMIT 50`. Measured span of the newest 50 eligible rows:

| Category | Effective window | Articles/48h | Grouped |
|---|---:|---:|---:|
| politics | **3.26 h** | 885 | 24 (2.7%) |
| sports | **3.67 h** | 1,089 | 10 (0.9%) |
| entertainment | 6.69 h | 743 | 11 (1.5%) |
| energy | 30.39 h | 67 | 20 (30%) |

The inverse correlation between volume and grouping is the signature. Same-event articles more than ~3h apart cannot appear in one prompt, so `#113`'s prediction that sports would approach its ~4.4% ceiling did not happen and could not have.

**2. The work is discarded.** `story_id = sha256(sorted member ids)[:16]` (`:210`), so *any* membership change mints a new row rather than updating one, and `:166-174` NULLs every `story_id` in the category window before re-assigning. Result: **58,259 of 58,557 `stories` rows (99.5%) have zero member articles.** Re-updated stories live 31–36 minutes — exactly one extra cycle — then churn.

**3. Cost scales with the wrong variable.** Because each run re-clusters the whole window, cost is O(cadence × window size) rather than O(new articles). Running every 30 minutes re-processes a near-identical slice six times over and keeps one result.

**Slowing the cadence was considered and rejected.** It would cut cost ~80% and leave grouping exactly as broken, because the window — not the frequency — is the binding constraint. It also trades away the 30-minute story latency the product wants. Fix the shape instead and both improve.

---

## The design: consume a queue, don't rescan a window

**The pivotal property: each new article searches *backward*; old articles are never re-examined.** A singleton from 20 hours ago is rescued when a matching new article arrives and pulls it in. That is what makes the work O(new articles ≈ 40/run) instead of O(window × categories), at unchanged cadence.

Everything needed is already in prod: **pgvector 0.8.0**, `articles.embedding` is a real `vector` column with an ivfflat cosine index, and every article has one (`null_emb: 0` across 8 days). Similarity search costs nothing.

Rewrite `workflows/story_workflow.py`:

1. **Queue, not window.** Select articles since a watermark in `pipeline_state`, replacing the `LIMIT 50` slice at `:43-57`.
2. **Free kNN candidate generation** for each new article against the full 48h in-category pool — not a 50-row slice. *This is the step that fixes grouping*: a sports article can now match a story 20 hours old.
3. **Branch on the result.**
   - Strong neighbour already in a story → attach to **that story's existing id**. No new row, no re-synthesis.
   - Strong mutual neighbours, none in a story → candidate new cluster.
   - Nothing above threshold → park. Costs $0, and needs no follow-up because future arrivals search backward over it.
4. **One batched LLM confirmation call per run**, across all categories, replacing ~5.4 `cluster_articles` calls per run.
5. **Synthesize only on material change.** A story gaining its Nth article from an outlet it already has does not need re-synthesis; a new outlet joining does. Reuses the ≥2-unique-outlets gate at `:196`. (The exact-duplicate case is already handled — PR #129.)
6. **Delete the blanket wipe** at `:166-174`. Stable story ids make it unnecessary, and removing it is what stops orphan accumulation at source.

Per run: ~5.4 cluster calls + ~23 synthesize calls → **~1 cluster call + 0–3 synthesize calls.**

---

## Threshold: 0.60, and the curve is why

Calibrated against **283 existing story-mate pairs** (articles the current LLM clusterer actually grouped) versus a 400-article random sample. `recall` = share of known-true pairs the gate would surface as candidates; `passthru` = share of articles reaching the LLM.

| Threshold | Recall | Pass-through | Est. threading $/day |
|---:|---:|---:|---:|
| **0.60** | **89.8%** | 57.0% | ~1.33 |
| 0.65 | 83.7% | 48.8% | ~1.14 |
| 0.70 | 74.2% | 43.5% | ~1.02 |
| 0.75 | 51.9% | 32.5% | ~0.76 |
| 0.80 | 32.5% | 26.0% | ~0.61 |
| 0.85 | 14.1% | 11.0% | ~0.26 |

**Ship at 0.60.** The cost curve is nearly flat where the recall curve is steep: moving 0.60 → 0.80 saves **$0.72/day** and destroys **57 points of recall**. There is no case for a strict threshold. An earlier draft of this design proposed 0.75–0.85; measuring the curve overturned it.

**Known bias:** these pairs were only ever found inside a 3.3h window, so the corpus under-represents exactly the long-range matches this design exists to catch. It is a sound starting point, not a final answer — `STATUS.md` Next-3 #1's 300-article corpus removes the bias when labelled, and now has a second payoff beyond its original purpose.

The `$/day` column scales today's $3.90 by pass-through. Treat it as a bound, not a forecast.

---

## Prerequisites — each is a thing this design would otherwise break silently

### P1. ~~Verify the vector index actually retrieves~~ — RESOLVED 2026-08-07, and it does not apply

**The design never touches `idx_articles_embedding`.** Measured with `EXPLAIN ANALYZE` against prod on sports, politics, entertainment and energy — all four plan identically:

```
Limit → Sort (top-N heapsort)  ← exact, not approximate
  → Bitmap Heap Scan on articles
      → Bitmap Index Scan on idx_articles_category_date
```

`category = $1 AND published_date > NOW() - 48h` is selective enough (64–1,103 rows) that the planner filters first and sorts cosine distance **exactly**. 8.5ms for the largest pool; 40 lookups in 2.5s.

So **recall is 100% by construction**, not an approximation to tune. The recall@10 bar, the `lists=20` rebuild and the `ivfflat.probes` question are all irrelevant to *this* query. They still matter for whole-corpus topic search (`sift/lib/db.ts:349`), which has no filter — but that is a separate concern and not a threading prerequisite.

This was written up as the scariest gate and turned out to be a non-issue. The lesson is the ordinary one: the plan asserted a dependency nobody had checked, and checking took one `EXPLAIN`.

### P1 (original text, retained for the record)

`idx_articles_embedding` is `ivfflat (embedding vector_cosine_ops) WITH (lists=20)` on **282,932 rows**. The rule of thumb is ≈√n ≈ **530**, so it is ~26× undertuned. The whole candidate gate depends on kNN returning true neighbours; if it does not, the gate under-performs while reporting success — the failure class `STATUS.md` was rebuilt around.

A probes=1 vs probes=20 spot check on 2026-08-05 was **inconclusive** — the sampled article had an exact duplicate at cosine 1.0, which any index finds. Do not treat the index as validated.

1. **Recall@10** on a 200-article sample vs exact brute force (`SET LOCAL enable_indexscan = off`). **Bar: ≥0.95.**
2. Rebuild at `lists ≈ 500`, or move to **HNSW** (supported in 0.8.0). Weigh HNSW's build time and RAM against Railway hobby tier.
3. **`ivfflat.probes` is query-time, not an index property.** Recall stays poor after any rebuild unless the query path sets it. This must land in code, not only in the migration.
4. Dual-migration per `CLAUDE.md` § Schema: `migrations/NNN_*.sql` with `CREATE INDEX CONCURRENTLY` **and** the non-concurrent idempotent twin in `app/db.py:_apply_migrations`, which is the path that actually applies on Railway.

Note: [NEON_RETENTION.md](./NEON_RETENTION.md) shrinks this table ~80% first, which makes the rebuild far cheaper. Sequence retention before the rebuild if both are happening.

### P2. SOLVED 2026-08-07 — by not using a watermark at all

The hole below is real, but the fix is simpler than the one proposed. `articles.threaded_at` (migration 017) is a **per-row marker**: NULL means queued. An article whose entities have not landed is simply not selected this run and stays NULL until they do.

There is no lag window to size, no catch-up sweep to remember, and no transactional watermark advance to get right — the failure mode is designed out rather than mitigated. Marking happens for *every* article the run considers, including ones that matched nothing, so a parked singleton stops being re-queued while remaining searchable as a neighbour.

Two things that fall out for free:

- **No backfill.** All ~280k historical rows are NULL, but the queue also bounds on the 48h window, so they are never selected and age further out, never in. Backfilling would rewrite 280k rows and cost ~60 MB of index bloat (see [NEON_RETENTION.md](./NEON_RETENTION.md)) for no behavioural difference.
- **No new index.** The working set is ~4,400 rows; `idx_articles_category_date` already serves the filter. A partial index on `threaded_at IS NULL` would span every historical row instead.

### P2 (original text, retained for the record)

`story_workflow.py:52` requires `jsonb_typeof(entities) = 'object'`, populated **asynchronously** by the batch poller. The current rescan *accidentally* protects against lag: an article skipped this run is seen again next run.

**A `created_at` watermark removes that protection.** An article whose entities have not landed is skipped, the watermark advances past it, and it is never reconsidered. Measured: **1.58% pending, 3.9 min average lag** → ~32 articles/day dropped permanently, compounding.

Fix: watermark on *entities-landed* rather than `created_at`, **or** keep a `created_at` watermark plus a bounded catch-up sweep for in-window rows with `story_id IS NULL` and entities now present. Either way, **advance the watermark only on successful completion, transactionally** — a run that crashes mid-way must not silently drop its batch.

This is correctness, not tuning. It blocks cutover.

### P3. Ship dark

`STATUS.md:21` — *"a detector that has never been run against a known-true case is an untested detector."* Same standard applies here.

1. Flag `INCREMENTAL_THREADING_ENABLED`, default **off**. (Note: this is the opposite default from `ENTITY_LINKER_REGEX_GATE_ENABLED`, and deliberately so — that one is a small, measured, reversible saving; this is a rewrite of the core product path.)
2. **24h shadow mode** — run read-only alongside the live path, logging what it *would* group without writing. Emit a structured event in the `cluster_stats` / `feed_stats` shape comparing decisions.
3. **Cutover bar:** grouped-article count ≥ current, and **no category regresses**. Sports and politics should *rise*.
4. Rollback is the flag. Keep the old path a full week post-cutover.

---

## Verification

| # | Check | Bar |
|---|---|---|
| 1 | ANN recall@10 vs brute force (P1) | ≥0.95 before cutover |
| 2 | Articles with entities landed but never threaded (P2) | stays 0 |
| 3 | Per-category articles-vs-grouped, 48h | rises; no category regresses |
| 4 | New `stories` rows with zero members | → ~0 (currently 99.5%) |
| 5 | Ingest → story-attachment latency | ≤35 min — the constraint this design exists to protect |
| 6 | `scripts/verify_cost_baseline.py` | threading $3.90/day → ~$1.33 |
| 7 | `pytest` + `scripts/explain_feed_queries.py` | green, including the index rebuild |

## What this deliberately does not do

- **Does not raise `LIMIT 50`.** The queue makes it irrelevant — there is no window scan left to bound.
- **Does not change cadence.** 30-minute freshness is a product requirement.
- **Does not try to fix grouping accuracy directly.** It removes the structural ceiling; whether the clusterer is *good* is Next-3 #1's labelled corpus, still unanswered.
