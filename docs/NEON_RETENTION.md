# Neon retention

**Status:** designed 2026-08-05, not started. `effort-day`.
**Companion:** [INCREMENTAL_THREADING.md](./INCREMENTAL_THREADING.md) — independent, but sequence retention *first* if both are happening.

This is a **different bill** from the Anthropic work in `STATUS.md` Open-Q #1. Nothing here reduces model spend.

---

## Measured

`pg_total_relation_size` / `pg_stat_user_indexes`, 2026-08-05:

| Object | Size | Note |
|---|---:|---|
| **Whole database** | **2,272 MB** | |
| `articles` | 2,089 MB | 316 MB heap + **1,020 MB indexes** |
| └ `idx_articles_embedding` | **879 MB** | **39% of the entire DB**; `idx_scan` = **75** lifetime |
| └ `embedding` column | 554 MB | 282,943 × 512-dim `vector` |
| `stories` | 124 MB | 99.5% orphan rows |
| `api_batches` | 48 MB total vs 6.6 MB heap + 1.3 MB idx | **~40 MB dead-tuple bloat** |

**The finding: 228,689 of 282,943 articles (80.8%) are older than 30 days and structurally unreachable.** Every feed query in `sift/lib/db.ts` has carried a 30-day recency floor since [sift#172](https://github.com/kristenmartino/sift/pull/172) (`STATUS.md` 2026-07-13), added because the importance × `EXP(-age)` sort could not be served by an index and was reading 29k–73k rows per category. The decay makes anything past ~30 days unrankable (~1e-13), so those rows cannot surface — but they still carry ~700 MB of embedding index.

Roughly four-fifths of Neon storage is data the product cannot display.

---

---

## EXECUTED 2026-08-05 — and the plan below was wrong

**Net for the day: 2,272 MB → 1,931 MB, −341 MB (−15%).** `idx_articles_embedding` 879 MB → **437 MB**. Tool: `scripts/prune_old_embeddings.py`, cutoff 90 days, 116,479 embeddings cleared, 167,039 kept.

Two cycles, because a full-corpus backfill ran between them:

| | db | `idx_articles_embedding` |
|---|---:|---:|
| start of day | 2,272 MB | 879 MB |
| prune + reindex | 1,934 MB | 436 MB |
| after `backfill_entity_links.py --include-empty` | 1,997 MB | **497 MB** |
| reindex again | **1,931 MB** | 437 MB |

**A full-corpus backfill costs ~60 MB of index bloat and needs a reindex afterwards.** `entity_links` is indexed (`idx_articles_entity_links_gin`), so ~280k UPDATEs cannot take the HOT path: every new row version adds an entry to *every* index on the table. The embedding index grew 436 → 497 MB purely from writes to an unrelated column. Reindex the GIN index too — it had its own bloat (6 MB, 1s).

Three things the original plan got wrong, all found by running its own safety check.

**1. Deleting rows was the wrong operation.** The plan said to delete articles past the feed's 30-day floor. Its own instruction — *confirm nothing outside the feed path reads older rows* — found that **three surfaces do, none with a date floor**: `sift/lib/db.ts:349` (topic search), `sift-mcp/server.py:234` (semantic search) and `:486` (`compare_outlets`). All scan the whole corpus by vector.

That reframed the operation. Article text is only ~100 MB; **63% of the DB was `embedding` plus its index**. So deleting rows buys ~100 MB more than clearing the column, while destroying `entity_links`, dossier references, `story_id` history, and the `source_url`/`content_hash` rows the deduplicator relies on — irreversibly.

Clearing the column gives up the *same* thing (vector reach over old articles) for nearly the same space, and is **safe because all four consumers already guard on `embedding IS NOT NULL`** — they exclude such rows rather than erroring. It is **reversible**: `backfill_embeddings.py` rebuilds from `title + summary`, both retained.

**2. `VACUUM` reclaimed nothing; `REINDEX` did all of it — twice.** Not a one-off:

| run | `VACUUM (ANALYZE) articles` | `REINDEX CONCURRENTLY` |
|---|---|---|
| after the prune | 127s, 2,423 → 2,425 MB (**+2**) | 42s, → 1,934 MB (**−491**) |
| after the backfill | 88s, 1,997 → 1,997 MB (**0**) | 61s, → 1,937 MB (**−60**) |

Plain VACUUM marks space *reusable*; it does not return it to Neon. **Reindex; do not expect VACUUM to do this job.** Only `VACUUM FULL` returns heap space, and it takes an exclusive lock.

**3. The 80.8%-unreachable figure is real but not directly bankable.** At the chosen 90-day cutoff only 41% of rows are touched, and the corpus is only ~4 months old, so "2.27 GB → ~500 MB" was never reachable at a cutoff safe for topic search.

**Still available**, in descending order: orphan `stories` (125 MB, 99.5% memberless — but do [INCREMENTAL_THREADING.md](./INCREMENTAL_THREADING.md) first or it refills), `api_batches` bloat (~40 MB), and `VACUUM FULL articles` for heap/TOAST bloat (exclusive lock, needs free space to rewrite).

**Measurement caveat:** taken immediately before a full `backfill_entity_links.py --include-empty` pass. `entity_links` is indexed, so those ~280k UPDATEs cannot take the HOT path and will add an entry to *every* index on the table, re-bloating the one just rebuilt. **Re-measure after that run**, and expect to reindex again.

---

## Actions, in order

### 1. Retention — archive, do not delete

Target rows past the feed's own 30-day floor. Models the DB from ~2.27 GB to **~500 MB**.

**This is destructive and irreversible. Do not run it unprompted.**

- `scripts/prune_old_articles.py`, dry-run by default, in the shape of `scripts/dedupe_outlet_profiles.py` and `regate_summaries.py` (idempotent, transactional, `--apply` to act).
- `COPY` to cold storage **before** any delete, and verify the archive is readable.
- Explicit sign-off from the owner, with the row count and the cutoff date stated.
- Decide the cutoff deliberately: the feed floor is 30 days, but `search_queries` retention is 90 (privacy commitment), and `story_workflow` only ever looks back 48h. 30 days is the smallest defensible number; 90 is the conservative one. **Recommend 90** — it costs ~200 MB more and removes the argument.

Check before running: nothing outside the feed path reads older rows. `scripts/eval_clustering.py --sample` pulls historical windows, and the clustering corpus (Next-3 #1) is drawn from them. **Label that corpus first, or exclude its article ids from the prune.**

### 2. Do not drop `idx_articles_embedding`

75 lifetime scans makes it look dead. It is not — [INCREMENTAL_THREADING.md](./INCREMENTAL_THREADING.md) makes it load-bearing, and the topic-search path in `sift` uses it. Retention shrinks it naturally; the P1 rebuild there (`lists=20` → ~500, or HNSW) then runs against a table ~80% smaller, which is the difference between a cheap rebuild and an expensive one. **Sequence retention before the rebuild.**

### 3. `VACUUM (FULL, ANALYZE) api_batches`

Reclaims ~40 MB of dead tuples. Takes an `ACCESS EXCLUSIVE` lock — run off-peak, and note the batch poller writes this table every 60s (`services/batch_poller.py:28`).

### 4. Prune orphan stories

58,259 rows with no member articles. Same script shape, dry-run default. Independent of the article prune, and **[INCREMENTAL_THREADING.md](./INCREMENTAL_THREADING.md) stops new ones forming** — do that first or this refills.

### 5. Re-measure and record

`pg_database_size` after each step, recorded the way `ai_usage_daily` now anchors the Anthropic side. A storage number in a doc goes stale exactly as fast as the "~$15/mo" in `STATUS.md:40` did.

---

## Still un-costed

Railway (hobby tier — `STATUS.md` Open-Q #1 tracks the scale trigger), Vercel, Clerk MAU, Sentry. There is no traffic (`search_queries`: **0 rows in 8 days**), so these sit near their floors. The real exposure is latent rather than current, and lives in the sibling repos:

1. **`sift/next.config.js:13`** — `remotePatterns: [{ protocol: "https", hostname: "**" }]`, no `minimumCacheTTL`. Any third party can bill the Vercel image quota through `/_next/image`. Narrow to the ~56 known outlet hosts.
2. **`sift-mcp/src/sift_mcp/server.py:57`** — `WEB_MAX_USES = 16` → up to **$0.16/call** in web-search fees, with no cost guard and no usage ledger in that repo. The Next.js equivalent caps at 2.
3. **`sift/app/api/news/topic/route.ts`** — unauthenticated, fires Haiku + web_search + Voyage; `lib/rate-limit.ts:1-4` is an in-memory Map that resets per cold start, and the budget guard defaults off and **fails open**.
