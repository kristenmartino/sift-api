# Source scaling

**Status:** measured 2026-08-11. Analysis only — no expansion executed.
**Companions:** [INCREMENTAL_THREADING.md](./INCREMENTAL_THREADING.md) (the threading cost this builds on), [NEON_RETENTION.md](./NEON_RETENTION.md) (the storage bill, which this shows is *not* the constraint).

The question: what happens to cost if we add 50, 100, or 1,000 outlets — to have more
to compare against, and to surface top-covered stories more cleanly?

**Short answer: +50 to +100 outlets is affordable (~$173–324/mo, from ~$135/mo). +1,000
breaks the architecture before it breaks the budget.**

Those figures are *post*-roster-narrowing, which shipped out of this analysis and took the
bill from $3.88 to **$2.69 per 1k articles (−31%)** — see
[What worked instead](#what-worked-instead-narrow-the-roster--shipped-2026-08-11). At the
old rate the same rows read $197 / $252–322 / $307–457.

---

## Measured

All figures from prod Neon, 7-day window ending 2026-08-11. Per-article rates come from
**2026-08-11 specifically** — it is the first clean UTC day after incremental threading
(#161) retired `story_clusterer.cluster`, so it is the only window not double-billing two
threading paths.

### Cost per 1,000 articles — $3.88 all-in

Quote `$/1k articles`, never `$/day`. `scripts/verify_cost_baseline.py` explains why at
length: the same workload on a 16%-busier day reported `summarizer.batch` at **+17.8%**
as a regression when volume-adjusted it was **−2.4%**.

| Group | $/1k articles | Scales with |
|---|---:|---|
| **Per-article stages** | **2.82** | article count, strictly linear |
| └ `entity_linker_llm.link_text` | 1.50 | |
| └ `summarizer.batch` | 0.65 | |
| └ `primer_generator.batch` | 0.33 | |
| └ `context_generator.batch` | 0.18 | |
| └ `entity_extractor.batch` | 0.16 | |
| └ `embedder.embed_texts` | ~0.00 | Voyage; negligible |
| **Threading** | **0.70** | articles × coverage depth |
| └ `story_synthesizer.synthesize` | 0.54 | |
| └ `story_confirmer.confirm` | 0.16 | batched /40 |
| **Compare** | **0.37** | user actions — **not** sources |

At the 7-day run rate of ~1,750 articles/day: **~$6.60/day, ~$197/mo.**

Against the frozen pre-optimization baseline of $5.38/1k articles (`verify_cost_baseline.py:80`),
that is **−28%**, and it is the number to scale from.

### The marginal outlet is not the average outlet

This is the correction that matters most, and getting it wrong moves the +100 estimate by
about 3×.

55 sources produce 1,757 articles/day. The naive average is 32/day/outlet. That average is
an artifact of skew:

| Rank | Source | Articles/day | Cumulative |
|---:|---|---:|---:|
| 1 | Sports Illustrated | 298.1 | 17.0% |
| 2 | New York Post | 247.8 | 31.1% |
| 3 | Fox News | 113.0 | 37.5% |
| 4 | Bloomberg | 86.9 | 42.5% |
| 5 | CBS News | 83.0 | 47.2% |
| … | | | |
| 10 | The Hill | 45.0 | **63.6%** |
| 25 | | | 87.0% |
| 50 | | | 99.5% |
| 55 | Carbon Brief | 1.0 | 100% |

- **top 10 sources = 64% of all volume**
- ranks 26–50 average **8.8/day**
- ranks 51–55 average **1.7/day**

Anything added now lands in the tail, because the head is already taken. **Model new
outlets at ~9 articles/day, not 32.**

### Scaling table

Ranges span marginal yield 9–20 articles/day/outlet and two assumptions about how fast
coverage depth grows with outlet count (see [the depth caveat](#the-depth-assumption-is-the-weakest-part)).

**Post-narrowing** (the shipped rate — $2.69/1k articles, linker at 0.31):

| Scenario | Outlets | Articles/day | Depth | $/mo | vs now | Threading queue |
|---|---:|---:|---:|---:|---:|---:|
| **Today** | 55 | 1,750 | 3.1 | **$135** | — | 18% |
| **+50** | 105 | 2,200–2,750 | 3.7–4.3 | **$173–224** | 1.3–1.7× | 29% |
| **+100** | 155 | 2,650–3,750 | 4.1–5.3 | **$212–324** | 1.6–2.4× | 39% |
| **+1000** | 1,055 | 10,750–21,750 | 6.6–12 | **$1,000–2,971** | 7–22× | **227%** |

At the pre-narrowing rate of $3.88/1k those read $197 / $252–322 / $307–457 / $1,384–3,747.
**Roster narrowing paid for roughly the first 50 outlets** — +50 now costs less than doing
nothing did a day earlier.

### Cost is not quadratic — the thing that looked dangerous isn't

Synthesis re-fires whenever a story gains a **new outlet**
(`workflows/incremental_threading.py:78 _attach`) and re-sends every member to the model.
Adding outlets is precisely the event that triggers it, so this looked like the blow-up
risk going in.

It is not. Deeper stories mean *fewer* stories for the same article count, and the two
effects largely cancel:

```
syn_calls   = stories × depth
stories     = threaded_articles / members     (members ∝ depth)
⇒ syn_calls ≈ threaded_articles × (depth / members) — and depth/members is ~constant
```

Confirmed against the ledger: 205 stories/day × 3.09 outlets/story = 633 predicted
synthesis calls; 652 measured. Synthesis grows from 9% of the bill to roughly 25% at
+1000 outlets, not 80%.

**The bill is ~85% linear in article count.** That is what makes the per-article stages,
not threading, the place to spend optimization effort.

---

## Ceilings — checked against prod, not assumed

### `MAX_QUEUE = 200`/run — not binding, leave it alone

`services/story_matcher.py:74`. 200 per run × 48 runs = 9,600 articles/day.

Over 7 days, bucketed into 30-minute windows: **busiest window 131 articles, p95 = 85,
mean 40.7, zero windows at cap.** It only binds above ~9,600/day — 5.5× the current rate,
reached only in the +1000 scenario. Raising it now is tuning against a number nothing is
touching.

### `MAX_ENTRIES_PER_FEED = 10` — binds on every feed, every run, and is *not* losing articles yet

`services/rss.py:156`. Per-feed, per-run.

**An earlier draft of this doc got this wrong and it is worth recording why.** It inferred
from stored rows that Sports Illustrated was "at or above the cap in 170 of 300 windows"
and concluded Sift was probably dropping SI articles today. Then the `feed_stats` event
(`services/rss.py:258`), which logs *fetched* counts, said something much starker:

```
feeds_ok=59 failed=0 empty=0 fetched=571
sources at or above cap 10: 55 of 55
```

**Every outlet fetches exactly 10 every run.** Washington Post gets 23 only because it has
four section feeds. So the cap binds universally — which makes the stored-row inference
useless as evidence of *loss*, because it cannot distinguish "the feed had more to give"
from "we took the top 10 of a feed that had not turned over".

The question that actually decides it is **turnover**: a feed only loses articles when it
publishes more than 10 in a 30-minute window, because the 11th scrolls off before the next
fetch. Against 48 runs × 10 slots:

| Source | stored/day | slots/day | turnover |
|---|---:|---:|---:|
| Sports Illustrated | 300.6 | 480 | **62.6%** |
| New York Post | 249.8 | 480 | 52.0% |
| Fox News | 114.0 | 480 | 23.8% |
| Bloomberg | 87.9 | 480 | 18.3% |
| CBS News | 83.6 | 480 | 17.4% |

**No feed is saturated. SI has ~1.6× headroom and everything else has far more.** So the
cap is not dropping articles on average today, and the constant should not be raised on
the strength of the earlier inference. Bursts are a different matter — a Sunday evening of
finals plausibly clears 10 in 30 minutes — but that is an argument for a per-feed cap, not
a global raise.

**It does matter for expansion, in the opposite direction from what was assumed.** The cap
means per-feed intake is bounded at 480/day no matter what an outlet publishes, so adding
high-volume outlets buys less than their raw rate suggests, and adding *many* outlets is
the only way volume grows. That is consistent with the ~9/day marginal yield above.

Note `rss.py:157` records that `FETCH_TIMEOUT` was raised for the WaPo section feeds in
#122; the entry cap itself has not been revisited. If it is ever raised, make it per-feed —
10 is right for Carbon Brief at 1/day and wrong for SI at 300/day.

### Storage is not the constraint

~8 KB/article all-in (2,272 MB / 282,943 articles). Even 10k articles/day at a 30-day
retention floor is ~2.4 GB. **Retention enforcement, not source count, is the storage
lever** — see [NEON_RETENTION.md](./NEON_RETENTION.md), where 80.8% of the corpus is
already past the feed's own 30-day floor.

### Fetch concurrency

`services/rss.py:223` fetches all feeds concurrently in one `asyncio.gather` with
`FETCH_TIMEOUT = 20.0`. Fine at 59 feeds. At 1,055 it is a different program, and the run
must still finish inside the 30-minute `REFRESH_INTERVAL` (`app/main.py:25`).

---

## More sources will not surface top-covered stories on their own

This was the second half of the original question, and it has a separate answer.

The feed's story pool (`sift/lib/db.ts:197`) ranks on:

```sql
ORDER BY (3 + 0.8 * LN(1 + COUNT(a.id)))::float * EXP(-age_days)
```

`COUNT(a.id)` is **raw article count, not distinct outlets**. Over 7 days of complete
stories, **29%** have more articles than outlets and **18%** are at ≥1.5× — one
high-volume outlet filing several pieces on one event. So a single outlet can manufacture
the corroboration the curve exists to measure.

That is worth fixing, and it has been (`outlet_count` end to end). **But it is not what
surfaces top-covered stories, and the first version of this section overstated it.**

**Correction.** An earlier draft said switching to distinct outlets moves "sports by 11.7
places on average, entertainment by 8.1". That was measured on the corroboration term
*alone*, with the decay factor dropped. The live query multiplies by
`EXP(-age_days)`, and replaying both orderings against prod with decay included moves
**0 of 20 in every category except politics, which moves one story**.

The reason is the real finding:

| sources | corroboration score |
|---:|---:|
| 2 | 3.879 |
| 5 | 4.433 |
| 18 | 5.356 |

The whole 2 → 18 range is a **1.38× spread**, while decay is exponential in days. So
**going from 2 outlets to 18 is worth 7.7 hours of freshness** — and the base constant `3`
is 77% of the score at n=2. Corroboration is very nearly not a ranking signal today;
recency is.

**So the lever is weighting, not the variable.** How many hours older an 18-outlet story
can be and still outrank a 2-outlet one:

| base | boost | hours |
|---:|---:|---:|
| 3 | 0.8 | **7.7** *(today)* |
| 3 | 1.6 | 11.6 |
| 3 | 3.0 | 15.1 |
| 1 | 0.8 | 13.9 |
| 1 | 1.6 | 17.5 |
| 0 | 1.0 | 23.7 |

Decay halves a score every 16.6 hours, for scale. Which row is right is a product call
about how much corroboration should outweigh freshness — not a mechanical fix, and
deliberately not made here.

The `ln` saturation itself is deliberate and documented (`sift/lib/db.ts:78-88`): it stops
an 18-member wire pile-up lapping a 6-outlet story 3×. That reasoning is right, and none
of the above changes it — the shape is correct, the variable was wrong, and the *scale*
against decay is the open question.

---

## What should happen first

Ordered by how much they change the value of a later expansion.

1. ~~**Make the entity linker cheaper**~~ — **done 2026-08-11.** It was 53% of per-article
   cost. Batching was the obvious move and was tried and rejected on measurement; **roster
   narrowing** shipped instead, at **−80% per call and −31% of the whole bill**, and it is
   *more* accurate rather than a trade. See
   [Batching the linker does not work](#batching-the-linker-does-not-work) below.

2. **Rank on distinct outlets** (`sift/lib/db.ts`, `sift/components/NewsAggregator.tsx`).
   Without it, added coverage raises the bill without becoming visible in the feed.

3. **Wire up the cost guard** (`services/cost_guard.py`). It currently reads as protection
   and is not: defaults off (`app/config.py:24`), neither env var set on Railway
   (`sift/docs/OPERATING_CONTEXT.md:89`), and `check_budget` called at only 2 of 8 paid
   call sites — `embedder.py:37` and the optional judge. The summarizer, linker, primer,
   extractor, clusterer, confirmer and synthesizer have no ceiling at all.

4. **Diagnose the per-feed cap.** Independent of expansion; see above.

Only then is expansion a config change rather than a project. Prefer wires (AP, Reuters,
AFP) and spectrum gaps over more outlets of the kind already carried — depth per dollar is
what makes compare and top-covered work, and the tail-yield numbers above say volume is
not what is being bought.

---

## Batching the linker does not work

**Measured 2026-08-11 and rejected.** This matters beyond the linker, because two places
in the repo carry batching as the safe fallback: `STATUS.md` calls it "modeled at −60%"
and `scripts/eval_linker_gate.py`'s ship-bar note recommends it as the alternative "which
has no recall risk". Both are now falsified.

Implemented as one call per 10 gated articles, returning a JSON object keyed by article
number, then A/B'd against the current one-call-per-article path on real gated prod
articles.

| Path | Links found | Exact-match articles | Recall vs single | Precision |
|---|---:|---:|---:|---:|
| **single run #2 (control)** | 73 | 97.0% | **97.3%** | 97.3% |
| batched, BATCH_SIZE=5 | 67 | 85.0% | 83.6% | 91.0% |
| batched, BATCH_SIZE=10 | 64 | 83.0% | 79.5% | 90.6% |

**The control is what makes this conclusive.** Run the single-article path twice over the
same 100 articles and it agrees with itself **97.3%** — the model is stable. So the
15–18 points batching gives up are real loss, not run-to-run noise. The first read of
this experiment was nearly the opposite conclusion, because batch-vs-single was measured
without ever measuring single-vs-single.

Two diagnostics ruled out the fixable explanations:

- **Not batch size.** Recall was 78.4% at BATCH_SIZE=2, 85.1% at 3 and 5, 82.4% at 10 —
  no trend. Even a batch of *two* loses ~20 points.
- **Not position.** Recall by slot within a batch of 10 ranged 66.7%–100% with no
  gradient, so it is not late articles being skimmed.

The loss is uniform, which points at the task framing itself: asking for N independent
extractions in one pass makes the model less thorough on each, and there is no batch size
or ordering that buys it back. Below the repo's 95% linker ship bar
(`eval_linker_gate.py:SHIP_BAR`), so it was reverted rather than shipped.

### What worked instead: narrow the roster — **shipped 2026-08-11**

The regex pre-gate already computes which catalog surface forms matched, and then throws
them away. The LLM's job, per its own docstring, is to *disambiguate* candidates, not
discover entities whose names never appear — so it does not need all 856 roster entries,
only the candidates plus their collision siblings.

`services/entity_linker.narrow_catalog`, behind
`entity_linker_roster_narrowing_enabled` (default on, kill switch).

**Measured end to end through `link_articles` against prod:**

| | Full roster | Narrowed |
|---|---:|---:|
| roster rows per call | 856 | **2.1 mean, 6 max** |
| input tokens per call | ~7,300 | **~650** |
| cost per call | $0.0042 | **$0.00086** (−80%) |
| linker, $/1k articles | 1.50 | **0.31** |
| **all-in, $/1k articles** | **3.88** | **2.69** |

**~$204/mo → ~$141/mo at the current run rate — 31% off the whole bill**, from the stage
that was 53% of per-article cost.

#### The accuracy check, and the bar that was wrong first

Diffing the narrowed path against the full one reads **94.2% recall, 85.3% precision**,
which looks like a downgrade. It is not, and the reason is that the diff treats the
incumbent as ground truth. The narrowed path finds *more* links, not fewer — so "85.3%
precision" only says 15% of its links are ones the full roster missed, not that they are
wrong.

`scripts/eval_linker_roster.py` answers it properly: run both paths, take the links they
disagree on plus a sample of the ones they agree on, and put each to Sonnet blind — the
adjudicator never sees which path proposed a tag, and the order is content-hashed. Over
400 gated articles:

| bucket | judged | correct | precision |
|---|---:|---:|---:|
| both paths agree | 60 | 51 | 85.0% |
| **full roster only** | 32 | 15 | **46.9%** |
| **narrowed only** | 44 | 31 | **70.5%** |

Pricing the agreed base at 85.0% and applying it to both:

| path | links | est. correct | precision |
|---|---:|---:|---:|
| full | 298 | 241.1 | 80.9% |
| **narrowed** | **310** | **257.1** | **82.9%** |

**Narrowing is more accurate, not a quality trade** — +2.0 points of precision *and* 16
more correct links. Where the two disagree, the narrowed path is right about half again
as often. Fewer distractors, better answers.

**The first version of this eval shipped on the wrong bar** and it is worth recording.
It scored only the disagreements, on "narrowed-unique precision ≥ full-unique precision",
and passed at 50.0% vs 42.9% on n=200. That bar ignores volume: the narrowed path produces
far more unique links, so a similar per-link rate still drags overall precision down. The
agreed base had to be priced before any of it meant anything. The corrected bar is
overall precision plus total correct links, and both had to hold.

---

## The depth assumption is the weakest part

Everything above is measured except one thing: how fast **coverage depth** (distinct
outlets per story, currently 3.14) grows as outlets are added. The model uses
`depth ∝ outlets^β` with β between 0.25 and 0.50, and that spread is most of the width of
the scaling table.

The cross-category evidence argues for the **low** end — depth barely tracks outlet count
in the range observed:

| Category | Outlets | Articles/day | Depth |
|---|---:|---:|---:|
| politics | 45 | 350 | 3.96 |
| entertainment | 46 | 340 | 2.59 |
| sports | 30 | 491 | 2.80 |
| business | 40 | 162 | 2.32 |
| world | 39 | 111 | 3.96 |
| science | 38 | 45 | 4.00 |
| energy | 34 | 25 | 1.62 |

Entertainment carries the most outlets (46) and nearly the lowest depth (2.59); science
has 38 outlets and the highest (4.00). Within this range depth is driven by what an event
is, not by how many outlets exist to cover it.

That is measured over 30–46 outlets per category, though, and cannot be extrapolated to
500. Treat the +1000 row as an order of magnitude, not a forecast.

---

## How to re-derive this

Do not trust the numbers above after any cost-affecting deploy. Re-run them.

**Spend** — `scripts/verify_cost_baseline.py`, which already prints `$/1k articles` and a
ratio-based deploy check that a quiet news day cannot fake:

```
./.venv/bin/python3 scripts/verify_cost_baseline.py --days 7
```

Read the `vol-adj` and `$/1k art` columns. Exit 3 means the window spans a threading
cutover and no verdict was issued.

**Per-source distribution** — the query at `scripts/audit_source_aliases.py:86`, or:

```sql
SELECT source_name, count(*)::float / GREATEST(count(DISTINCT created_at::date), 1) AS per_day
FROM articles WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY source_name ORDER BY per_day DESC;
```

**Coverage depth** —

```sql
SELECT avg(k) FROM (
  SELECT story_id, count(DISTINCT source_name) k FROM articles
  WHERE story_id IS NOT NULL AND created_at >= CURRENT_DATE - INTERVAL '7 days'
  GROUP BY story_id) t;
```

**Queue headroom** — 30-minute buckets against `MAX_QUEUE`:

```sql
SELECT max(n), percentile_cont(0.95) WITHIN GROUP (ORDER BY n), avg(n)
FROM (SELECT date_trunc('hour', created_at)
             + interval '30 min' * floor(extract(minute from created_at)/30) b,
             count(*) n
      FROM articles WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
      GROUP BY 1) t;
```

**Per-source rhythm and feed health** — `services/feed_health.py:80`.
