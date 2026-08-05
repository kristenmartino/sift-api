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
| `audit_unlinked_entities.py`    | No (writes CSVs) | No        | Ranks `articles.entities` person/org mentions that have no row in any profile table, by article count and by `articles.category`, plus a regex sweep for bill mentions (the extractor emits none) and top unmatched `search_queries.query_norm`. Reports the extraction denominator first and refuses to rank below `--min-extraction-rate`. Anti-joins the **catalog**, not `entity_links` — the `entity_links` delta is reported separately as a linker-tuning signal. Outputs `data/unlinked_entity_suggestions.csv`, `data/unlinked_entities_by_category.csv`, `data/unmatched_search_queries.csv` for human review. No LLM calls. |
| `scrape_govtrack.py`            | No (writes a CSV) | No        | Phase 3.B one-shot: pulls every current Senator + Representative from the public GovTrack API and writes a fresh `data/politician_profiles.csv` (~536 rows). Preserves hand-curated `committees`, `notes`, and non-GovTrack `external_links` keys across re-runs by `bioguide_id`. Re-run quarterly to refresh names / parties / leadership. No API key required. |
| `scrape_committees.py`          | No (updates the CSV) | No     | Phase 3.F.1: enriches `data/politician_profiles.csv` with committee assignments from the canonical `unitedstates/congress-legislators` YAMLs. Top-level committees only (subcommittees skipped to keep dossier lists tight). Strips chamber-prefix boilerplate ("Senate Committee on Finance" → "Finance"). Re-run-safe — only updates the `committees` field. No API key required. |
| `import_opensecrets_bulk.py`    | No (updates the CSV) | No     | Phase 3.F.2 (bulk path): aggregates PAC contributions from `data/opensecrets/pacs22.txt` (gitignored — see `data/opensecrets/` directory note below) and updates `top_industries_current_cycle` on `politician_profiles.csv`. Filters administrative codes (refunds, party transfers) via the `CRP_Categories.txt` "Sector Long" classification. Re-run quarterly when OpenSecrets releases new bulk data. **Replaces the discontinued OpenSecrets API.** No key required. |
| `seed_politician_profiles.py`   | **Yes**       | No           | UPSERTs `politician_profiles` from `data/politician_profiles.csv`. Idempotent. |
| `seed_org_profiles.py`          | **Yes**       | No           | UPSERTs `org_profiles` from `data/org_profiles.csv`. Idempotent. |
| `seed_bill_profiles.py`         | **Yes**       | No           | UPSERTs `bill_profiles` from `data/bill_profiles.csv`. Idempotent. NULLs out unresolved sponsor_bioguide refs. |
| `seed_entity_aliases.py`        | **Yes**       | No           | UPSERTs `entity_aliases` (migration 014) from `data/entity_aliases.csv` — curated surface forms like "Pentagon" → `united-states-department-of-defense`. Validates every row against the live catalog and drops any that (1) targets a missing dossier, (2) falls below `_MIN_CURATED_KEY_LENGTH` (2 — curated rows are exempt from the 4-char `_MIN_KEY_LENGTH` that suppresses *derived* keys, which is what lets `bbc`/`cnn`/`npr` link at all) or is a stopword, (3) is another entity's canonical name, or (4) collides with another profile name in a way `link_text`'s longest-match-wins cannot resolve — a person's surname ("Kennedy") or a shared head noun ("Times"), but *not* mere containment in a longer name ("Congress" inside "Library of Congress"). Idempotent; `--dry-run` and `--prune` supported (the only seeder that can prune today). |
| `seed_all.sh`                   | **Yes**       | No           | One-shot wrapper: dry-run validates every CSV, then runs all six seeds against prod in order, with a human-review pause before the alias seed. `--dry-run-only` and `--skip-aliases` flags supported. |
| `backfill_entity_links.py`      | **Yes**       | Only with `--mode llm` | Re-runs the entity linker over stored articles and writes corrected links back. **Default (no flags): already-linked rows via the LLM path** — the historical behavior, used after #40 dropped last-name-only aliases. **`--include-empty` widens it to every article**, which is what applies migration 014's curated aliases to the ~92% that never had a chip; that mode defaults to `--mode regex` (free, deterministic, and where aliases live via `build_search_dict`). LLM mode over >1,000 articles refuses without `--yes` — the empty set would be roughly $260 at ~$0.001/article. Processes in chunks with a write per chunk, so an interrupted run keeps its progress; idempotent, so re-running resumes. `--limit`, `--chunk-size`, `--dry-run` supported. |
| `scrape_executive_records.py`   | No (writes a CSV) | No        | Phase 4: gathers the primary records behind the executive dossiers. Reads senate.gov roll-call vote menus (free, unmetered, **contiguous 111th–119th by default** — `build_executive_profiles.py` infers a former official's end date from the successor's confirmation and refuses to infer across a Congress that was never read) and enriches with `receivedDate` + the `"vice <predecessor>"` clause from api.congress.gov. Set `CONGRESS_API_KEY` (free, instant) or it falls back to `DEMO_KEY` at ~10 req/hour; every page is cached under `data/.congress_cache/` so a slow run is resumable and a re-run is free. `--skip-congress-api` gets the roll-call half with no key. Output: `data/executive_confirmations.csv`. |
| `build_executive_profiles.py`   | No (writes a CSV) | No        | Joins `data/executive_offices.csv` (office → statutory title + source + official .gov link, authored once per office) and `data/executive_assignments.csv` (person → office, PN citation, or archives.gov term dates) against the scraped confirmations into `data/executive_profiles.csv`, the artifact a human reviews. Withholds `role_title` from anyone succeeded in an office whose handover falls outside the scraped range — rendering the title with no end date would assert they still hold it. Fills `predecessor_name` from the nomination's `"vice <name>"` clause where it exists (2 of 37 — en-bloc transition Cabinet filings have none), else from the Senate's previous confirmation to the office, recording which in `predecessor_source` because they are different claims. |
| `verify_role_sources.py`        | No (writes a CSV) | No        | Refetches every distinct `role_title_source` and asserts the record literally names the office. Strips uscode.house.gov's editorial/statutory notes first — 42 U.S.C. §4321 mentions "Administrator of the Environmental Protection Agency" inside a 2022 appropriations note, so a naive substring test "verifies" a section that does not establish the office — and rejects a match preceded by Deputy/Associate/Assistant/Under. Output: `data/role_source_verification.csv`. `seed_executive_records.py` refuses to write a row this did not mark OK. |
| `seed_executive_records.py`     | **Yes**       | No           | Migration 015. In one transaction: clears the uncited `notes` prose on all 102 `chamber IN ('executive','foreign-executive')` rows, writes the verified role provenance, and adds `external_links.official` where the row lacks one (a row that already points at a specific office page keeps it). Aborts if the verification report is older than the profiles CSV. Idempotent; `--dry-run` supported. `notes` on the 536 sitting-Congress rows is untouched. |
| `cleanup_old_search_queries.py` | **Yes** (DELETE) | No        | Phase 1 search-analytics retention. DELETEs `search_queries` rows older than 90 days (configurable via `--days N`). The privacy page commits to 90-day retention on logged search queries; this script enforces it. Re-run weekly or wire into a daily Railway cron when query volume warrants. `--dry-run` flag for spot-checks. Safe to re-run. |
| `cleanup_old_primer_events.py`  | **Yes** (DELETE) | No        | Sister to `cleanup_old_search_queries.py` — same shape, same retention promise, different table. DELETEs `primer_expand_events` rows older than 90 days. Run on the same cadence. |

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
