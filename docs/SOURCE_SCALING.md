# Source scaling

**Status:** measured 2026-08-11. Analysis only — no expansion executed.
**Companions:** [INCREMENTAL_THREADING.md](./INCREMENTAL_THREADING.md) (the threading cost this builds on), [NEON_RETENTION.md](./NEON_RETENTION.md) (the storage bill, which this shows is *not* the constraint).

The question: what happens to cost if we add 50, 100, or 1,000 outlets — to have more
to compare against, and to surface top-covered stories more cleanly?

**Short answer: +50 to +100 outlets is affordable (~$250–460/mo, from ~$197/mo). +1,000
breaks the architecture before it breaks the budget.** But two things should land first,
and neither is about cost ceilings — see [What should happen first](#what-should-happen-first).

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

| Scenario | Outlets | Articles/day | Depth | $/mo | vs now | Threading queue |
|---|---:|---:|---:|---:|---:|---:|
| **Today** | 55 | 1,750 | 3.1 | **$197** | — | 18% |
| **+50** | 105 | 2,200–2,750 | 3.7–4.3 | **$252–322** | 1.3–1.6× | 29% |
| **+100** | 155 | 2,650–3,750 | 4.1–5.3 | **$307–457** | 1.6–2.3× | 39% |
| **+1000** | 1,055 | 10,750–21,750 | 6.6–12 | **$1,384–3,747** | 7–19× | **227%** |

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

### `MAX_ENTRIES_PER_FEED = 10` — probably binding *today*

`services/rss.py:156`. Per-feed, per-run.

| Source | max/window | windows ≥10 | windows | mean |
|---|---:|---:|---:|---:|
| Sports Illustrated | 23 | **170** | 300 | 7.95 |
| New York Post | 17 | 37 | 321 | 6.17 |
| CBS News | 12 | 4 | 213 | 3.12 |
| Washington Examiner | 11 | 4 | 192 | 3.39 |

Sports Illustrated sits at or above the cap in **57% of windows**. That strongly suggests
Sift is dropping SI articles right now, independent of any expansion.

**This cannot be proven from stored rows** — truncated articles were never written, and
`created_at` buckets do not align exactly with pipeline runs, which is why a window can
show 23. Confirm from the `feed_stats` event (`services/rss.py:258`), which logs
`articles_by_source` as *fetched*. Only then change the constant.

Note `rss.py:157` records that `FETCH_TIMEOUT` was raised for the WaPo section feeds in
#122; the entry cap has not been revisited since. If confirmed, the fix is likely a
per-feed cap rather than one global constant — 10 is right for Carbon Brief at 1/day and
wrong for SI at 298/day.

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

`COUNT(a.id)` is **raw article count, not distinct outlets**. Measured over 7 days of
complete stories:

- **29%** have more articles than outlets; **18%** at ≥1.5×
- re-ranking on `COUNT(DISTINCT a.source_name)` moves **sports by 11.7 places on average**
  (73 of 86 stories shift ≥5) and **entertainment by 8.1** (99 of 107 shift ≥5)
- politics 2.7, business 3.5, world 0.9, health and science ~0.4

The distortion is concentrated exactly where the high-volume outlets live — SI, NY Post,
Fox — and **adding sources makes it worse**, because more sources means more articles per
story without necessarily more outlets per story.

The `ln` saturation itself is deliberate and documented (`sift/lib/db.ts:78-88`): it exists
to stop an 18-member wire pile-up lapping a 6-outlet story 3×. That reasoning is right.
It is saturating the correct way on the wrong variable.

---

## What should happen first

Ordered by how much they change the value of a later expansion.

1. **Batch the entity linker** (`services/entity_linker_llm.py`). It is **53% of the
   per-article cost and the only paid stage with no batching** — `link_articles_llm:428`
   fans out one call per article at concurrency 4, each re-sending a ~7K-token catalog
   that is only affordable because of the ephemeral cache at `entity_linker_llm.py:399`.
   The regex pre-gate (#130) already removed ~74% of calls; batching the survivors ~10/call
   attacks what is left. This makes every future source cheaper, permanently.

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
