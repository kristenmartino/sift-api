# scripts/

One-off and diagnostic scripts. All run from the `sift-api/` root and use
`DATABASE_URL` from the environment (falling back to `app.config.settings`).

| Script                      | Writes to DB? | Costs money? | Purpose                                                                 |
| --------------------------- | ------------- | ------------ | ----------------------------------------------------------------------- |
| `backfill_context.py`       | **Yes**       | **Yes** (Anthropic) | One-shot: fill in `why_it_matters` + `importance_score` on existing articles that are missing them. Calls Claude per article. |
| `explain_feed_queries.py`   | No (read-only) | No           | Runs `EXPLAIN (ANALYZE, BUFFERS)` across all 10 categories × 3 feed query shapes. Exits non-zero on plan regression. Wired into the `feed-perf` CI job. |

## Running against prod

The Railway container does **not** ship with `psql`. For ad-hoc Python tooling
against the prod DB, use the local venv under `railway run`:

```bash
railway run ./.venv/bin/python3 scripts/explain_feed_queries.py
```

For scripts that write (like `backfill_context.py`), prefer running them once
locally with the prod `DATABASE_URL` exported, so you can Ctrl-C cleanly.

## Adding a new script

- Put the file here; name it for what it does, not for what it uses.
- Keep the `sys.path.insert(...)` shim at the top so the script runs from any CWD.
- Handle Neon's `ssl=require` explicitly: `"require" if "neon.tech" in db_url else False`.
- Add a row to the table above, honestly marking the writes/cost columns.
- If the script should gate deploys, wire it into `.github/workflows/ci.yml`
  (see `feed-perf` for the pattern).
