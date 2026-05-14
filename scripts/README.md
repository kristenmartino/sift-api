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
| `scrape_govtrack.py`            | No (writes a CSV) | No        | Phase 3.B one-shot: pulls every current Senator + Representative from the public GovTrack API and writes a fresh `data/politician_profiles.csv` (~536 rows). Preserves hand-curated `committees`, `notes`, and non-GovTrack `external_links` keys across re-runs by `bioguide_id`. Re-run quarterly to refresh names / parties / leadership. No API key required. |
| `scrape_committees.py`          | No (updates the CSV) | No     | Phase 3.F.1: enriches `data/politician_profiles.csv` with committee assignments from the canonical `unitedstates/congress-legislators` YAMLs. Top-level committees only (subcommittees skipped to keep dossier lists tight). Strips chamber-prefix boilerplate ("Senate Committee on Finance" → "Finance"). Re-run-safe — only updates the `committees` field. No API key required. |
| `import_opensecrets_bulk.py`    | No (updates the CSV) | No     | Phase 3.F.2 (bulk path): aggregates PAC contributions from `data/opensecrets/pacs22.txt` (gitignored — see `data/opensecrets/` directory note below) and updates `top_industries_current_cycle` on `politician_profiles.csv`. Filters administrative codes (refunds, party transfers) via the `CRP_Categories.txt` "Sector Long" classification. Re-run quarterly when OpenSecrets releases new bulk data. **Replaces the discontinued OpenSecrets API.** No key required. |
| `seed_politician_profiles.py`   | **Yes**       | No           | UPSERTs `politician_profiles` from `data/politician_profiles.csv`. Idempotent. |
| `seed_org_profiles.py`          | **Yes**       | No           | UPSERTs `org_profiles` from `data/org_profiles.csv`. Idempotent. |
| `seed_bill_profiles.py`         | **Yes**       | No           | UPSERTs `bill_profiles` from `data/bill_profiles.csv`. Idempotent. NULLs out unresolved sponsor_bioguide refs. |
| `seed_all.sh`                   | **Yes**       | No           | One-shot wrapper: dry-run validates every CSV, then runs all six seeds against prod in order, with a human-review pause before the alias seed. `--dry-run-only` and `--skip-aliases` flags supported. |
| `backfill_entity_links.py`      | **Yes**       | No           | One-shot: re-runs the Phase 3.G entity linker over articles that already have a non-empty `entity_links` value, writes corrected links back. Used after #40 dropped last-name-only aliases (the policy change cleared 46 of 50 false-positive chips in prod). Idempotent — safe to re-run; `--dry-run` for spot checks. Regex-only, no LLM cost. |
| `cleanup_old_search_queries.py` | **Yes** (DELETE) | No        | Phase 1 search-analytics retention. DELETEs `search_queries` rows older than 90 days (configurable via `--days N`). The privacy page commits to 90-day retention on logged search queries; this script enforces it. Re-run weekly or wire into a daily Railway cron when query volume warrants. `--dry-run` flag for spot-checks. Safe to re-run. |

## Running against prod

The Railway container does **not** ship with `psql`. For ad-hoc Python tooling
against the prod DB, use the local venv under `railway run`:

```bash
railway run ./.venv/bin/python3 scripts/explain_feed_queries.py
```

For scripts that write (like `backfill_context.py`), prefer running them once
locally with the prod `DATABASE_URL` exported, so you can Ctrl-C cleanly.

## Refresh cadence (politician_profiles)

The civic-literacy data on `politician_profiles` doesn't go stale at the
same rate. No cron — manual re-runs on the schedule below.

| Surface              | Source                                  | Cadence                                          | Trigger                                          |
| -------------------- | --------------------------------------- | ------------------------------------------------ | ------------------------------------------------ |
| Roster (536 members) | GovTrack public API                     | ~Every 6 months                                  | Special elections / mid-term changes             |
| Committees           | unitedstates/congress-legislators YAMLs | Twice a year; **must** rerun in Jan of odd years | New Congress seats Jan 3 of odd years            |
| Top industries (PAC) | OpenSecrets bulk data                   | Once per cycle (~every 2 years)                  | New cycle bulk drops ~6 months after cycle close |

**Why no cron**: OpenSecrets discontinued their public API on 2025-04-15,
which removed the only daily-refresh use case. Committees alone change a
few times a year — daily/weekly is overkill; manual handles it. See
sift-api PR #32 for the abandoned scheduler.

**Re-run sequence (any of the above)**:

```bash
# 1. Refresh source-of-truth CSV (run only the one(s) you need)
./.venv/bin/python3 scripts/scrape_govtrack.py          # roster
./.venv/bin/python3 scripts/scrape_committees.py        # committees
./.venv/bin/python3 scripts/import_opensecrets_bulk.py  # PAC industries

# 2. Commit the CSV diff (review with `git diff` first)

# 3. Seed against prod
railway run ./.venv/bin/python3 scripts/seed_politician_profiles.py
```

When you bump the OpenSecrets cycle (2022 → 2024), also update the cycle
label in the sift frontend: `lib/copy.ts → topIndustries: "Top industries
by PAC contributions (YYYY cycle)"`.

`data/opensecrets/` is gitignored (CC NC-SA license). On a fresh clone,
download `pacsXX.txt` + `CRP_Categories.txt` from
`opensecrets.org/open-data/bulk-data` into `data/opensecrets/` before
running `import_opensecrets_bulk.py`.

## Adding a new script

- Put the file here; name it for what it does, not for what it uses.
- Keep the `sys.path.insert(...)` shim at the top so the script runs from any CWD.
- Handle Neon's `ssl=require` explicitly: `"require" if "neon.tech" in db_url else False`.
- Add a row to the table above, honestly marking the writes/cost columns.
- If the script should gate deploys, wire it into `.github/workflows/ci.yml`
  (see `feed-perf` for the pattern).
