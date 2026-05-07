# scripts/

One-off and diagnostic scripts. All run from the `sift-api/` root and use
`DATABASE_URL` from the environment (falling back to `app.config.settings`).

| Script                          | Writes to DB? | Costs money? | Purpose                                                                 |
| ------------------------------- | ------------- | ------------ | ----------------------------------------------------------------------- |
| `backfill_context.py`           | **Yes**       | **Yes** (Anthropic) | One-shot: fill in `why_it_matters` + `importance_score` on existing articles that are missing them. Calls Claude per article. |
| `explain_feed_queries.py`       | No (read-only) | No           | Runs `EXPLAIN (ANALYZE, BUFFERS)` across all 10 categories × 3 feed query shapes. Exits non-zero on plan regression. Wired into the `feed-perf` CI job. |
| `seed_outlet_profiles.py`       | **Yes**       | No           | UPSERTs `outlet_profiles` from `data/outlet_profiles.csv`. Idempotent. |
| `audit_source_aliases.py`       | No (writes a CSV) | No        | Lists distinct unmapped `articles.source_name` values + suggested matches. Output: `data/source_alias_suggestions.csv` for human review. |
| `seed_source_aliases.py`        | **Yes**       | No           | UPSERTs `source_name_aliases` from a reviewed suggestions CSV. Idempotent. |
| `scrape_committees.py`          | No (updates the CSV) | No     | Phase 3.F.1: enriches `data/politician_profiles.csv` with committee assignments from the canonical `unitedstates/congress-legislators` YAMLs. Top-level committees only (subcommittees skipped to keep dossier lists tight). Strips chamber-prefix boilerplate ("Senate Committee on Finance" → "Finance"). Re-run-safe — only updates the `committees` field. No API key required. |
| `seed_politician_profiles.py`   | **Yes**       | No           | UPSERTs `politician_profiles` from `data/politician_profiles.csv`. Idempotent. |
| `seed_org_profiles.py`          | **Yes**       | No           | UPSERTs `org_profiles` from `data/org_profiles.csv`. Idempotent. |
| `seed_bill_profiles.py`         | **Yes**       | No           | UPSERTs `bill_profiles` from `data/bill_profiles.csv`. Idempotent. NULLs out unresolved sponsor_bioguide refs. |
| `seed_all.sh`                   | **Yes**       | No           | One-shot wrapper: dry-run validates every CSV, then runs all six seeds against prod in order, with a human-review pause before the alias seed. `--dry-run-only` and `--skip-aliases` flags supported. |

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
