# CLAUDE.md — orientation for Claude Code (and future-me)

Context you'll want before editing anything here. Keep this file **short and current** — if it grows past one screen, split the long bits into real docs.

## Pre-session ritual

Before doing real work in a session:

1. Read [`STATUS.md`](./STATUS.md) — Active focus, Open question, Next 3, Blocked-on, Recent decisions.
2. List open PRs + issues (`mcp__github__list_pull_requests` / `list_issues`, or `gh` locally).
3. If touching the feed read path, also read `sift/lib/db.ts` in the sibling repo — those queries are the user-visible slow path, not anything here.

If `STATUS.md` is older than ~3 days during a high-velocity period (10+ PRs / week), flag the staleness to the user before starting.

## End-of-PR doc-impact check

Before opening the PR:

- Did this change anything in `STATUS.md`'s Next 3, Blocked-on, or Open question? Update it.
- Did this make or close a strategic decision? Add a `## Recent decisions` entry in `STATUS.md` (and, if substantial, a row in the sibling `sift/docs/DECISIONS.md`).
- Did this change a public contract (API endpoint, response shape, env var)? Update `README.md` and any `sift/docs/TECHNICAL_SPEC.md` rows that referenced it.
- Did this change DB schema? Update `init.sql` AND add a `migrations/NNN_*.sql` AND extend `app/db.py:_apply_migrations`. See `## Schema` below.
- Run the sift-api–specific `## Before closing a task` checklist at the bottom of this file.

## The two-repo split

The product lives in two sibling repos under `sift_v1/`:

| Repo                                          | Role                                            |
| --------------------------------------------- | ----------------------------------------------- |
| `sift/` (Next.js 14, TypeScript, Vercel)      | User-facing frontend **and read path**.         |
| `sift-api/` (FastAPI, Python 3.12, Railway)   | Background pipeline, compare workflow, **write path**. |

Both talk to the **same Neon Postgres**. They are independent git repos — do not try to `git add` across them.

**Consequence**: the queries that users wait on (the `/api/news` feed) live in **`sift/lib/db.ts`**, not anywhere in `sift-api/`. When optimizing user-facing reads, read `sift/lib/db.ts` first, then come back here to add indexes / migrations.

## Where the slow path actually is

Client → Next.js API route → `sift/lib/db.ts` → Postgres.

- Client abort budget: `API_TIMEOUT_MS = 10_000` in `sift/lib/constants.ts`. Exceeding it surfaces as "We hit a snag pulling today's stories / Request timed out." (set in `sift/lib/hooks.ts`).
- There is no retry; one timeout = one error UI.

### Feed queries and the indexes that serve them

All feed queries are in `sift/lib/db.ts`. Partial indexes are defined in `migrations/004_feed_indexes.sql` and re-applied at startup by `app/db.py:_apply_migrations`.

| Query (sift/lib/db.ts) | Purpose                    | Index                                              |
| ---------------------- | -------------------------- | -------------------------------------------------- |
| `:36` getArticlesByCategory | category articles fallback | `idx_articles_feed`                                |
| `:85` stories + LEFT JOIN   | top stories per category   | `idx_stories_feed` + `idx_articles_story_feed`     |
| `:121` story articles       | fetch articles for stories | `idx_articles_story_feed`                          |
| `:150` standalone articles  | articles outside stories   | `idx_articles_feed`                                |

Diagnostic: `python scripts/explain_feed_queries.py` (runs EXPLAIN ANALYZE against all 30 query shapes, warns ≥ 2000 ms, fails ≥ 8000 ms). Also wired into CI as the `feed-perf` job — see below.

## Schema

Source of truth for a fresh DB: `init.sql`. **Additive** changes layer on via two mechanisms:

1. **`migrations/NNN_*.sql`** — `CREATE INDEX CONCURRENTLY IF NOT EXISTS` etc. These are for operators applying changes to a live DB manually (CONCURRENTLY cannot run in a transaction).
2. **`app/db.py:_apply_migrations`** — the same DDL, non-CONCURRENTLY, idempotent via `IF NOT EXISTS`. Runs at FastAPI startup. **This is the path that actually applies changes on Railway.**

When adding a migration: write it in both places. The SQL file is documentation + manual ops; the Python hook is the prod apply path.

## Running scripts against prod

- `psql` is **not** installed in the Railway container. Use `railway run ./.venv/bin/python3 ...` with asyncpg for one-off SQL.
- Local Python: `./sift-api/.venv/bin/python3` (system python has no asyncpg).
- Neon requires `ssl=require`. `scripts/explain_feed_queries.py` handles this already via `"neon.tech" in db_url` check.
- `railway status` should show project **`sift`**, service `sift-api`, environment `production` (verified 2026-08-05; this line said `fortunate-charisma` until then, which is Railway's auto-generated name and was presumably renamed at some point).
- Railway **auto-deploys `main`, but gates on CI** — a merge shows `Waiting for CI` and only then `Building`. Do not conclude a change is live from the merge, or unlive from a status check taken too soon: `railway status --json` carries `latestDeployment.meta.commitHash`, which is the only direct answer.

## CI

`.github/workflows/ci.yml` has two jobs (this said "three" until 2026-08-05, while the table below it listed two):

| Job         | Trigger                                        | Needs                          |
| ----------- | ---------------------------------------------- | ------------------------------ |
| `lint-test` | every PR + push to main                        | none                           |
| `feed-perf` | PRs touching `migrations/`, `init.sql`, or `scripts/explain_feed_queries.py`; plus `workflow_dispatch` and the `feed-perf` label | `DATABASE_URL` repo secret (prod Neon URL) |

`feed-perf` uses an **in-job git diff** rather than workflow-level `paths:`, so it still reports a status on every PR — important for branch protection's required-check semantics.

**`app/db.py` was in that trigger list until 2026-08-14 and is not any more.** The job runs `EXPLAIN ANALYZE` against production, and most of `app/db.py` is pool wiring and helpers that cannot move a plan — so editing any of it woke the prod compute for nothing. Dropping it is safe because of a convention the file already documents (see the comments above the feed-index and entity-link `CREATE INDEX` calls in `_apply_migrations`): **every index created there is also mirrored into a `migrations/*.sql` file.** That convention is now load-bearing for CI — add plan-relevant DDL to `_apply_migrations` without a matching `migrations/` file and the gate will not fire. Use the `feed-perf` label or `workflow_dispatch` when you need it on an `app/db.py`-only change.

## Things I've tripped on

- `sift-api` and `sift` commits are separate. A "push the branch" request is usually `sift-api` only; confirm before touching `sift/`.
- `sift/docs/` has big product specs (FEATURE_SPECS.md is 2400+ lines). They're useful for product intent, not for where-does-X-live questions. Code reading is faster.
- The pool in `sift/lib/db.ts` has `max: 5` — don't raise casually; Neon caps connections by compute size.
- **Never add a polling loop that queries Postgres on a timer.** Neon scales to zero after 300s without a query, so any interval shorter than that pins the compute on 24/7 — invisibly: no error, no latency, no failing test, just an invoice. The batch poller did exactly this for months (see `docs/DECISIONS.md` D54 in `sift/`). If a loop needs to know something, check whether this process already knows it — the in-flight batch set and `last_pipeline_run` both turned out to be in memory. Verify with `scripts/verify_neon_idle.py --probe`.
- When Railway logs show a healthcheck pass but the UI still times out, the queries are the problem, not the deploy. Look at the feed queries.

## Where to file new work (decision tree)

When you discover something during a session that's worth tracking, use this to decide where it goes. The goal: **never lose anything, but don't over-file** either.

| What you found | Where it goes |
|---|---|
| **Bug blocking current work** | Fix in active branch. Don't file. |
| **Concrete feature committing to in next ~2 weeks** | GitHub issue with `tier-v1.5` / `tier-v2` + `effort-*` labels. Add to STATUS.md "Next 3" if it bumps something. |
| **Concrete feature wanted eventually, no commitment** | Note in `STATUS.md` "Recent decisions" if it's a decision; otherwise wait until you're ready to commit, then file an issue. |
| **Quirk or minor bug, not urgent** | GitHub issue with `bug` label. No need to surface in STATUS.md unless it blocks Next 3. |
| **Critical bug found but not fixed** | GitHub issue with `bug` label, then mention in STATUS.md "Blocked-on" if it blocks Next 3. |
| **Strategic question / open architectural decision** | STATUS.md "Open strategic question" — never a GitHub issue. Questions get answered through usage/conversation, not engineering work. |
| **Architectural decision now made** | STATUS.md "Recent decisions" with a date. If substantial, also add a row in [`sift/docs/DECISIONS.md`](https://github.com/kristenmartino/sift/blob/main/docs/DECISIONS.md) (sift owns the cross-repo decision log). |
| **Out-of-scope idea surfaced during work** | If it's tied to a specific file, use the spawned-task chip in your editor. Otherwise note in STATUS.md "Recent decisions" or open an issue if scoped. |

**The rule:** dated + scoped → file an issue. Half-formed → leave in STATUS.md context or a casual note. Issues you'll never close are noise.

## Before closing a task

- If I changed a query or index, rerun `scripts/explain_feed_queries.py` against prod.
- If I edited `app/db.py` migrations, verify via `railway logs --service sift-api` that startup ran clean.
- If I edited `.github/workflows/`, verify on a small PR that the job actually runs (don't assume path filters work).
- Plus the universal end-of-PR doc-impact check at the top of this file.
