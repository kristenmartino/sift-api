# scripts/

One-off and diagnostic scripts. All run from the `sift-api/` root and use
`DATABASE_URL` from the environment (falling back to `app.config.settings`).

| Script                          | Writes to DB? | Costs money? | Purpose                                                                 |
| ------------------------------- | ------------- | ------------ | ----------------------------------------------------------------------- |
| `backfill_context.py`           | **Yes**       | **Yes** (Anthropic) | One-shot: fill in `why_it_matters` + `importance_score` on existing articles that are missing them. Calls Claude per article. |
| `explain_feed_queries.py`       | No (read-only) | No           | Runs `EXPLAIN (ANALYZE, BUFFERS)` across all 10 categories × 3 feed query shapes. Exits non-zero on plan regression. Wired into the `feed-perf` CI job. |
| `verify_cost_baseline.py`       | No (read-only) | No          | Per-operation $/day vs the frozen 2026-07-31..08-04 baseline, plus a **deploy check** that reads call *ratios* so it can tell "not deployed" from "deployed and saved nothing". Read the `vol-adj` column, not `raw` — a busier day inflates raw spend and once showed a `-2.4%` operation as `+17.8%`. `$/1k articles` is volume-free and the best single figure to quote. **Which threading path is live is decided by `articles.threaded_at`, not the ledger**: shadow mode bills `story_confirmer.confirm` identically to the live path, and keying off that reported incremental threading live on 2026-08-07, three days before cutover. Exit **0** all checks pass, **2** something that should be live is not, **3** the window spans the 2026-08-10 cutover so no threading verdict was issued — narrow `--since`. `--days`, `--since`, `--json` supported. |
| `output_stop_summary.py`        | No (read-only) | No          | Reads `llm_output_stops` (migration 021): how each Claude call *ended*, split on whether its response passed index alignment. **Built to test whether `summarizer.batch`'s alignment re-asks were output truncation; answered 2026-08-11, they are not.** Over 212 calls: 0 ended in `max_tokens` (all `end_turn`), peak output 481 of 700, and **1 misaligned call — 0.5%**, not the 4-12% the investigation was premised on. That 4-12% was an artifact of inferring re-asks as excess over `ceil(articles / BATCH_SIZE)` in `ai_usage_daily`, which counts every partial last-batch as a retry (18 of 212 calls ran short, carrying 43 articles that would pack into 9 calls). **Still worth running when swapping the model behind any batched operation**: the aligned/misaligned split is the only stored signal for whether a model returns parseable indexed JSON at all, and it is what caught gpt-5-nano emitting 30/30 empty batches at the same ceiling with zero provider errors. `--days` supported. |
| `seed_outlet_profiles.py`       | **Yes**       | No           | UPSERTs `outlet_profiles` from `data/outlet_profiles.csv`. Idempotent. |
| `audit_source_aliases.py`       | No (writes a CSV) | No        | Lists distinct unmapped `articles.source_name` values + suggested matches. Output: `data/source_alias_suggestions.csv` for human review. |
| `seed_source_aliases.py`        | **Yes**       | No           | UPSERTs `source_name_aliases` from a reviewed suggestions CSV. Idempotent. |
| `audit_unlinked_entities.py`    | No (writes CSVs) | No        | Ranks `articles.entities` person/org mentions that have no row in any profile table, by article count and by `articles.category`, plus a regex sweep for bill mentions (the extractor emits none) and top unmatched `search_queries.query_norm`. Reports the extraction denominator first and refuses to rank below `--min-extraction-rate`. Anti-joins the **catalog**, not `entity_links` — the `entity_links` delta is reported separately as a linker-tuning signal. Outputs `data/unlinked_entity_suggestions.csv`, `data/unlinked_entities_by_category.csv`, `data/unmatched_search_queries.csv` for human review. No LLM calls. |
| `scrape_govtrack.py`            | No (writes a CSV) | No        | Phase 3.B one-shot: pulls every current Senator + Representative from the public GovTrack API and writes a fresh `data/politician_profiles.csv` (537 rows as of 2026-08-07). Preserves hand-curated `committees`, `notes`, and non-GovTrack `external_links` keys across re-runs by `bioguide_id`. Re-run quarterly to refresh names / parties / leadership. No API key required. **GovTrack's later pages are slow** — measured 0.5s / 20.5s / 14.7s for offsets 0/200/400 on 2026-08-07 — so the client allows 120s and retries twice; the original 30s bound failed most runs on page two or three and read as a flaky network. |
| `scrape_committees.py`          | No (updates the CSV) | No     | Phase 3.F.1: enriches `data/politician_profiles.csv` with committee assignments from the canonical `unitedstates/congress-legislators` YAMLs. Top-level committees only (subcommittees skipped to keep dossier lists tight). Strips chamber-prefix boilerplate ("Senate Committee on Finance" → "Finance"). Re-run-safe — only updates the `committees` field. No API key required. |
| `import_opensecrets_bulk.py`    | No (updates the CSV) | No     | Phase 3.F.2 (bulk path): aggregates PAC contributions from `data/opensecrets/pacs22.txt` (gitignored — see `data/opensecrets/` directory note below) and updates `top_industries_current_cycle` on `politician_profiles.csv`. Filters administrative codes (refunds, party transfers) via the `CRP_Categories.txt` "Sector Long" classification. Re-run quarterly when OpenSecrets releases new bulk data. **Replaces the discontinued OpenSecrets API.** No key required. |
| `seed_politician_profiles.py`   | **Yes**       | No           | UPSERTs `politician_profiles` from `data/politician_profiles.csv`. Idempotent. **Does not own `interest_group_ratings`** — that column belongs to `seed_lcv_scores.py`, and the roster CSV still carries the pre-019 `{}` for every row, so a plain assignment would wipe it (532 LCV entries were one run away from this on 2026-08-07). The upsert keeps a non-empty stored array and otherwise takes the incoming value, which is coerced to `[]`; guarding on the *incoming* value instead would make a bad stored value immortal. `--prune` **retires rather than deletes**: a sitting member absent from the CSV gets `chamber='former'`, which `lib/publishFloor.ts` already withholds from the sitemap. Never DELETEs — a politician row is referenced by `articles.entity_links` (26,706 chips on one member alone) and carries LCV scores and curated notes this script does not own. Scoped to `house`/`senate`, because the CSV is the current-Congress roster and the 112 executive / foreign-executive / scotus rows are absent from it by design. `--prune --dry-run` lists who would be retired, with their article-chip count. |
| `seed_org_profiles.py`          | **Yes**       | No           | UPSERTs `org_profiles` from `data/org_profiles.csv`. Idempotent. |
| `seed_bill_profiles.py`         | **Yes**       | No           | UPSERTs `bill_profiles` from `data/bill_profiles.csv`. Idempotent. NULLs out unresolved sponsor_bioguide refs. |
| `scrape_lcv_scorecard.py`      | No (writes a CSV) | No        | Scrapes the LCV National Environmental Scorecard into `data/lcv_scores.csv` — 542 members, 2025 + lifetime, each with LCV's own per-member URL. Chamber is inferred from `congress-district`, which LCV emits only for House rows; the year is read from the markup, never assumed. Re-run annually when LCV publishes. No API key. |
| `seed_lcv_scores.py`           | **Yes**       | No           | UPSERTs LCV entries into `politician_profiles.interest_group_ratings` (migration 019 array shape) from `data/lcv_scores.csv`. Matches LCV names to bioguide ids in three tiers — token-equal (diacritics folded), subset, then unique surname+state+chamber — and **reports rather than guesses** anything unresolved (532/542 matched 2026-08-07). Replaces this rater's entry, preserves others'. Idempotent; `--dry-run`. |
| `seed_entity_aliases.py`        | **Yes**       | No           | UPSERTs `entity_aliases` (migration 014) from `data/entity_aliases.csv` — curated surface forms like "Pentagon" → `united-states-department-of-defense`. Validates every row against the live catalog and drops any that (1) targets a missing dossier, (2) falls below `_MIN_CURATED_KEY_LENGTH` (2 — curated rows are exempt from the 4-char `_MIN_KEY_LENGTH` that suppresses *derived* keys, which is what lets `bbc`/`cnn`/`npr` link at all) or is a stopword, (3) is another entity's canonical name, or (4) collides with another profile name in a way `link_text`'s longest-match-wins cannot resolve — a person's surname ("Kennedy") or a shared head noun ("Times"), but *not* mere containment in a longer name ("Congress" inside "Library of Congress"). Idempotent; `--dry-run` and `--prune` supported (the only seeder that can prune today). |
| `seed_all.sh`                   | **Yes**       | No           | One-shot wrapper: dry-run validates every CSV, then runs all six seeds against prod in order, with a human-review pause before the alias seed. `--dry-run-only` and `--skip-aliases` flags supported. |
| `backfill_entity_links.py`      | **Yes**       | Only with `--mode llm` | Re-runs the entity linker over stored articles and writes corrected links back. **Default (no flags): already-linked rows via the LLM path** — the historical behavior, used after #40 dropped last-name-only aliases. **`--include-empty` widens it to every article**, which is what applies migration 014's curated aliases to the ~92% that never had a chip; that mode defaults to `--mode regex` (free, deterministic, and where aliases live via `build_search_dict`). LLM mode over >1,000 articles refuses without `--yes` — the empty set would be roughly $260 at ~$0.001/article. **LLM mode overwrites, so it writes only rows the model actually answered for**: a failed call (timeout, API error, garbled response) leaves the stored links alone and is reported as `skipped` — re-run to retry it. Before that guard, a wave of 8s timeouts read as "no entities" and emptied 218 rows on 2026-08-05. Processes in chunks with a write per chunk, so an interrupted run keeps its progress; idempotent, so re-running resumes. `--limit`, `--chunk-size`, `--dry-run` supported. |
| `scrape_executive_records.py`   | No (writes a CSV) | No        | Phase 4: gathers the primary records behind the executive dossiers. Reads senate.gov roll-call vote menus (free, unmetered, **contiguous 111th–119th by default** — `build_executive_profiles.py` infers a former official's end date from the successor's confirmation and refuses to infer across a Congress that was never read) and enriches with `receivedDate` + the `"vice <predecessor>"` clause from api.congress.gov. Set `CONGRESS_API_KEY` (free, instant) or it falls back to `DEMO_KEY` at ~10 req/hour; every page is cached under `data/.congress_cache/` so a slow run is resumable and a re-run is free. `--skip-congress-api` gets the roll-call half with no key. Output: `data/executive_confirmations.csv`. |
| `build_executive_profiles.py`   | No (writes a CSV) | No        | Joins `data/executive_offices.csv` (office → statutory title + source + official .gov link, authored once per office) and `data/executive_assignments.csv` (person → office, PN citation, or archives.gov term dates) against the scraped confirmations into `data/executive_profiles.csv`, the artifact a human reviews. Withholds `role_title` from anyone succeeded in an office whose handover falls outside the scraped range — rendering the title with no end date would assert they still hold it. Fills `predecessor_name` from the nomination's `"vice <name>"` clause where it exists (2 of 37 — en-bloc transition Cabinet filings have none), else from the Senate's previous confirmation to the office, recording which in `predecessor_source` because they are different claims. |
| `verify_role_sources.py`        | No (writes a CSV) | No        | Refetches every distinct `role_title_source` and asserts the record literally names the office. Strips uscode.house.gov's editorial/statutory notes first — 42 U.S.C. §4321 mentions "Administrator of the Environmental Protection Agency" inside a 2022 appropriations note, so a naive substring test "verifies" a section that does not establish the office — and rejects a match preceded by Deputy/Associate/Assistant/Under. Rows carrying `verify_name` must additionally be *named* by the page: a U.S. statute establishes an office without naming anyone (the officeholder comes from a roll-call), but a foreign government's page is the only record there, so it has to carry both halves. Output: `data/role_source_verification.csv`. `seed_executive_records.py` refuses to write a row this did not mark OK. **A pass is a filter, not sign-off** — it tests that two strings are present, not that the page makes the claim, and `iletisim.gov.tr` passed on a syndicated press-clipping feed. Read the context before publishing a row. |
| `seed_executive_records.py`     | **Yes**       | No           | Migration 015. In one transaction: clears the uncited `notes` prose on all 102 `chamber IN ('executive','foreign-executive')` rows, writes the verified role provenance, and adds `external_links.official` where the row lacks one (a row that already points at a specific office page keeps it). Aborts if the verification report is older than the profiles CSV. Idempotent; `--dry-run` supported. `notes` on the 536 sitting-Congress rows is untouched. |
| `prune_orphan_stories.py`      | **Yes** (DELETE) | No        | DELETEs `stories` rows no article points at and older than 48h. They exist because `story_workflow` derived `story_id` from a sha256 of its members, so any membership change minted a new row and orphaned the old one — 99.5% of the table. Incremental threading (#158/#161/#180) stopped that, and pruning before the cutover would have refilled within hours. Three guards: `articles_story_id_fkey` has no `ON DELETE`, so Postgres **refuses** to delete a story an article still points at (a bad WHERE clause can only fail loudly); the 48h floor exceeds threading's own lookback, so nothing mid-assignment is touched; and `--apply` archives every row to gitignored `data/_cache/orphan_stories/*.jsonl` and aborts if the count mismatches. Dry-run default, chunked, idempotent. **Run `VACUUM FULL stories` afterwards** — on 2026-08-10 deleting 60,020 of 61,143 rows returned only 10 MB until the rewrite, which then took the table 132 MB → 2.2 MB in 0.1s. |
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

## Audit snapshots

`audit_unlinked_entities.py` writes undated working files to `data/`. Those are
gitignored — they are regenerable, and the big one is ~1.2 MB, so leaving them
tracked would churn the repo on every run.

To keep a result, copy it to a dated name and commit that:

```bash
railway run ./.venv/bin/python3 scripts/audit_unlinked_entities.py
cd data && for f in unlinked_entity_suggestions unlinked_entities_by_category \
                    unmatched_search_queries; do
  cp "$f.csv" "$f.$(date +%F).csv"
done
```

Same convention as `data/org_profiles.prod-backup-2026-07-27.csv`.

### 2026-08-05 — the run that motivated migration 014

Baseline for judging whether the alias layer moved anything. Against 282,931
articles, 207,935 with entities extracted (73.5%):

| | |
|---|---|
| Articles carrying any `entity_links` | **21,614 — 7.6%** |
| Distinct mentions with no catalog row | 13,556 |
| Unlinked mention-articles | 233,364 |
| ...of which sports + entertainment | **157,557 (67%)** — outside the civic frame |
| Catalog at the time | 838 rows |

Top civic gaps: Trump Administration (2,787), Democratic Party (1,690),
OpenAI (1,218), Apple (1,145), Graham Platner (1,082), SpaceX (957),
Republican Party (839), Pentagon (756 — a dossier existed, the *name* did not,
which is what `entity_aliases` fixes).

Two caveats when reading the CSV. The `substring` tier is a review heuristic,
not data: it suggested "Iran" → `EXEC-MIRAN-S` and "U.S." →
`us-marshals-service`, so it must never be bulk-imported. And
`unmatched_search_queries` returned 4 rows at one search each — the search
signal is not usable at current traffic.

## Refresh cadence (politician_profiles)

The civic-literacy data on `politician_profiles` doesn't go stale at the
same rate. No cron — manual re-runs on the schedule below.

| Surface              | Source                                  | Cadence                                          | Trigger                                          |
| -------------------- | --------------------------------------- | ------------------------------------------------ | ------------------------------------------------ |
| Roster (537 members) | GovTrack public API                     | ~Every 6 months                                  | Special elections / mid-term changes. Follow with `seed_politician_profiles.py --prune` — the scrape adds arrivals, only `--prune` retires departures. |
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
