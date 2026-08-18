# Verifying the `world` misfile fix — method, traps, and decision rule

**Status: measurement pending.** The fix (#263) is deployed; the post-deploy read has not been
taken. A scheduled task executes the procedure below and records the result. This file is the
durable record — the task prompt is a copy that can be lost or recreated.

## What was wrong, and what shipped

Issue **#227**: US-domestic stories were being filed into the `world` category. Over a 7-day
window (n=819) the **New York Post was the single largest contributor to the `world` tab —
98 articles, ahead of BBC World's 57.** What landed there was stray-cat, murder-suicide and
shark-attack copy. Category is assigned by the summarizer, not the feed
(`services/summarizer.py`, same call that writes the summary), and `FALLBACK_CATEGORY =
"general"` catches unrecognised labels — so these were *confident* misclassifications, not
fallbacks.

**#263** ("Expose 'general' as a real classification choice") fixed it, merge commit `2f779d7`,
deployed to prod **2026-08-18T03:26:43Z**. It changed the classifier prompt, its tests, and
added `scripts/eval_world_misfiles.py`. **It shipped no migration and no backfill** — a fact the
whole verification method rests on.

### Pre-fix baseline

From #263's own PR body, `--mode diagnose`, default 7-day window, before the fix:

| | misfile rate |
|---|---|
| New York Post | **44%** (n=105, sampled 30) |
| General-firehose average (9 sources incl. NY Post) | 8.9% |
| BBC World (dedicated feed) | 8.0% |

Other general sources (CBS, Washington Examiner, Fox, BBC, Bloomberg, NYT, PBS, FT) sat at 0–8%.

## Why this is hard to measure

**Classification is written once at ingest and existing rows are never re-classified.** Because
#263 shipped no backfill, every row created before the deploy keeps its pre-fix category forever.
So a naive "run the diagnostic and compare" does not measure the fix — it re-measures the old
data, and the trailing-window default makes that failure *look* like a clean result.

Two readings were taken that demonstrate both halves of the trap. Neither is a valid basis for
any conclusion about the fix.

**Reading 1 — unfiltered, 32 minutes post-deploy.** Returned NY Post 40% (n=105), general-firehose
avg 9.2%, BBC World 6.7%. Every figure is within noise of the pre-fix baseline, because the
trailing 7-day window was still ~100% pre-fix rows. The tell was the *identical* n=105: the same
underlying rows, measured twice. Reported as "the fix didn't work," this would have been a false
negative that reopened a correctly-closed issue.

**Reading 2 — `--since`-filtered, ~35 minutes post-deploy.** Isolated 4 post-fix `world` rows
system-wide, none from the four target sources. Too early to read — but it is the **empirical
proof that the filter is exact**, and therefore the reference point for interpreting a small n
later: *few rows under `--since` means low post-fix volume, not a broken filter.*

## The measurement tool

**#266** added `--since <ISO-8601>` to the diagnose mode (`origin/main`, `41e2897`). It is an
**extra** filter on top of `--days`, not a replacement: `AND created_at >= $n::timestamptz` is
ANDed into both the volume query and the per-source sample query, so the effective window is the
intersection and `--since` dominates whenever it is the tighter bound.

`articles.created_at` (`TIMESTAMPTZ DEFAULT NOW()`) is the **exact** discriminator between rows
classified by the pre-fix and post-fix prompt. `published_date` — the outlet's own byline time —
says nothing about which classifier ran, and sorting by it does not isolate post-fix rows.

Verified rather than assumed: parameter numbering is correct in both the with- and
without-`--since` branches (the `f"${len(params)+1}"` construction is the usual place this breaks
silently), both predicates are genuinely ANDed, and `--since 2026-08-18T03:26:43Z` parses to a
UTC-aware datetime on the project venv's **Python 3.11.15** — the `Z` suffix requires 3.11+, and
would raise `ValueError` on 3.10 or earlier.

## Window mismatch — what compares and what does not

`--since` covers only the time since deploy; the baseline covers a full 7 days. That asymmetry
makes some comparisons valid and others meaningless:

- **Misfile rates compare directly.** They are proportions of a sample; window length does not
  affect them.
- **Raw volume counts do not.** The baseline's `n=105` is a 7-day count. A shorter post-fix window
  yields a smaller count by arithmetic, not by improvement. Reading that shrinkage as success is
  the most likely remaining error.
- **Normalized volume is a genuine second signal.** In articles/day (baseline NY Post ≈ 105/7 ≈
  15/day), NY Post's `world` volume *should fall*, because misfiled articles now classify as
  `general`. **If the rate drops but per-day volume does not, that is a finding** — it would mean
  articles are being retained in `world` by a failure #263 did not address.
- **Tab-composition ordering needs no normalization.** "Is NY Post still ahead of BBC World?"
  compares sources inside one window. That ordering is the original framing of #227 and is the
  most legible single result.

Any reported volume figure should carry its window span and the `--since` value beside it.

## Decision rule for #226

**#226** proposed a low-importance ranking dampener for `world`, and was deferred by #263's scope
note pending this measurement. The rule agreed in advance:

- NY Post's rate drops meaningfully toward the general-firehose average, and that average stays
  comparable to or better than BBC World's dedicated-feed rate → the classification fix is
  sufficient; **#226 stays closed as superseded.**
- NY Post or the general-firehose average remains materially elevated → ranking-level mitigation
  is still warranted; **reopen #226** with the specific source, rate, and what is still slipping
  through.

Either way the numbers get posted to #226. **"Not enough post-fix volume yet" is a legitimate
outcome** — a deferral, not a failure — and is preferable to a verdict on thin data. This is
measurement and recommendation only; no ranking code changes follow from it directly.

## Re-running by hand

From a fresh worktree off `origin/main`, linked to Railway
(`railway link -p 5bbf8184-1f72-4572-9531-d8ed2401b0c0 -e 4787362e-e4ee-4553-9b65-7f98ba367361 -s 0d35d4ab-06b6-42e8-a8c2-2ea7ef68e8fa`),
per CLAUDE.md's prod-script policy — no local `psql`:

```
railway run <venv>/python3 scripts/eval_world_misfiles.py \
  --mode diagnose --days 7 --limit 30 \
  --since 2026-08-18T03:26:43Z --json <scratch>/eval_result.json
```

Confirm first that the deployed commit is a **descendant of `2f779d7`** (`git merge-base
--is-ancestor 2f779d7 <deployed-sha>`). Expect a SHA well after `3fd9964` — `main` has moved, and
a redeploy on a newer commit is normal. A deployed commit that is *not* a descendant means the fix
was rolled back, which is the only failing case.

> `3fd9964` was prod HEAD at deploy time, but it is **#245's** merge commit ("Calibrate the
> faithfulness judge"), which already contained #263. #263's own merge is `2f779d7`. Do not label
> `3fd9964` as "#263" in STATUS or an issue comment.
