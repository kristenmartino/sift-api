# CLAUDE.md — orientation for Claude Code (and future-me)

Context you'll want before editing anything here. Keep this file **short and current** — if it grows past one screen, split the long bits into real docs.

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
- `railway status` should show project `fortunate-charisma`, service `sift-api`, environment `production`.

## CI

`.github/workflows/ci.yml` has three jobs:

| Job         | Trigger                                        | Needs                          |
| ----------- | ---------------------------------------------- | ------------------------------ |
| `lint-test` | every PR + push to main                        | none                           |
| `feed-perf` | PRs touching `app/db.py`, `migrations/`, or `scripts/explain_feed_queries.py` | `DATABASE_URL` repo secret (prod Neon URL) |

`feed-perf` uses an **in-job git diff** rather than workflow-level `paths:`, so it still reports a status on every PR — important for branch protection's required-check semantics.

## Things I've tripped on

- `sift-api` and `sift` commits are separate. A "push the branch" request is usually `sift-api` only; confirm before touching `sift/`.
- `sift/docs/` has big product specs (FEATURE_SPECS.md is 2400+ lines). They're useful for product intent, not for where-does-X-live questions. Code reading is faster.
- The pool in `sift/lib/db.ts` has `max: 5` — don't raise casually; Neon free/hobby tiers cap connections.
- When Railway logs show a healthcheck pass but the UI still times out, the queries are the problem, not the deploy. Look at the feed queries.

## Before closing a task

- If I changed a query or index, rerun `scripts/explain_feed_queries.py` against prod.
- If I edited `app/db.py` migrations, verify via `railway logs --service sift-api` that startup ran clean.
- If I edited `.github/workflows/`, verify on a small PR that the job actually runs (don't assume path filters work).
