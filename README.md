# sift-api

Python FastAPI + LangGraph backend for [Sift](https://siftnews.kristenmartino.ai) — the AI-curated news reader.

Handles the background content pipeline (RSS feeds → Claude Haiku summaries → Voyage AI embeddings → Neon Postgres) and the multi-source comparison workflow (LangGraph fan-out web search → claim extraction → comparison).

## Architecture

```
Railway asyncio scheduler (every 30 min)
  → LangGraph pipeline: fetch_rss → deduplicate → summarize (Claude) → context/primer (Claude, batch) → embed (Voyage) → link_entities → store (Postgres)
      → nested LangGraph story-threading (per category): fetch_articles → extract_entities → cluster (Claude) → synthesize_and_store

User compare request (via Vercel proxy)
  → LangGraph compare: search_sources (parallel, Claude web_search) → extract_and_compare → format_response
```

This service compiles **three** LangGraph `StateGraph`s: the ingestion **pipeline**, a **story-threading** graph the pipeline invokes per category, and the on-demand **compare** graph. (Two are user-facing workflows; story-threading is nested inside ingestion.)

User-facing reads happen in the Next.js frontend — this service handles background AI processing and on-demand comparison.

## Content quality gate

All new article and story copy follows the shared standard in
`services/copy_rules.py`; see [Feed copy rules](docs/COPY_RULES.md) for the
generation paths, write-time safeguards and regression fixtures. `/health`
reports `copy_rules_version` so production can be checked directly. The checks
cover article and story summaries, optional significance notes, primer
background/definitions and per-source story notes.

The prompts require supported facts, useful explanations and empty optional notes
when nothing distinct is available. Deterministic checks run before publication:
article and story summaries cannot introduce numeric figures absent from their
inputs; recognizable filler and guessed outlet omissions are removed. Story
notes must copy a contiguous source passage of at most 25 words and add detail
beyond the shared summary. All real sources retain a row, even with a blank note.

Both primer paths share a parser that checks the article reference, removes
repetition, limits terms and withholds unsupported figures or prior-plan claims.
Automatic `why_it_matters` notes are withheld: the classification stage receives
only the headline and summary and has no independent evidence for another fact.
Importance, tone and genre still update. Existing editorially reviewed notes are
not bulk-cleared. Optional backgrounds and surviving source passages receive
focused Sonnet novelty/redundancy review; unavailable or budget-blocked review leaves notes blank. This check is
metered through the existing judge cost ledger. The legacy `WHY_IT_MATTERS_JUDGE_ENABLED` setting cannot override
this evidence requirement; `services/judge.py` remains an offline evaluation tool.

These are conservative safeguards, not a complete factual or semantic validator.
Exact live-review and live-model failures, plus useful-copy retention controls,
run in the full CI suite on every PR and push to main.

```bash
# Eval: baseline existing copy vs. the new rubric, before/after rates (judge = API spend)
python scripts/eval_why_it_matters.py                                   # offline, fixture corpus, gate only
python scripts/eval_why_it_matters.py --from-db --limit 500 --judge --mode compare

# Re-gate already-stored copy (ON CONFLICT does not regenerate, so audit-era
# bad lines persist until reprocessed). Dry run by default.
python scripts/regate_existing.py            # preview drops
python scripts/regate_existing.py --apply    # NULL failing lines / blank cliché backgrounds (terms kept)
```

## Setup

### Prerequisites

- Python 3.12+
- Docker (for local Postgres + pgvector)

### Local development

```bash
# Start Postgres
docker compose up -d

# Create virtualenv and install deps (dev file pulls in requirements.txt too)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Run server
uvicorn app.main:app --reload --port 8000
```

### Verify

```bash
# Health check
curl http://localhost:8000/health

# Trigger the RSS pipeline (refreshes all categories; there is no category-scoped refresh)
curl -X POST http://localhost:8000/pipeline/refresh \
  -H "Content-Type: application/json" \
  -H "X-Pipeline-Key: dev-key" \
  -d '{"force": true}'

# Multi-source comparison
curl -X POST http://localhost:8000/analyze/compare \
  -H "Content-Type: application/json" \
  -H "X-Pipeline-Key: dev-key" \
  -d '{"topic": "Federal Reserve interest rate decision", "sources": ["reuters", "bbc", "associated press"]}'
```

## API

All endpoints are available at both `/v1/...` (preferred) and legacy paths (for backwards compatibility).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Service info + available endpoints |
| GET | `/health` | Health check + DB status + last pipeline run + `copy_rules_version`. Also carries `model_overrides` when `LLM_MODEL_OVERRIDES` has moved a stage off its default model — omitted entirely when nothing has |
| GET | `/docs` | Interactive API documentation (Swagger UI) |
| GET | `/redoc` | Alternative API documentation (ReDoc) |
| POST | `/v1/pipeline/refresh` | Trigger RSS pipeline (auth required) |
| POST | `/v1/analyze/compare` | Multi-source comparison via LangGraph |
| POST | `/v1/analyze/compare/stream` | Same comparison as SSE: `stage` / `source-done` / `results` / `error` events |
| POST | `/v1/analyze/compare/daily/refresh` | Force-regenerate the anonymous daily compare example (auth required) |

## Project structure

```
sift-api/
├── app/
│   ├── main.py              # FastAPI app, health, background scheduler
│   ├── config.py            # pydantic-settings
│   ├── db.py                # asyncpg connection pool
│   ├── models.py            # Pydantic schemas
│   └── routers/
│       ├── pipeline.py      # POST /pipeline/refresh
│       └── compare.py       # POST /analyze/compare
├── workflows/
│   ├── pipeline_workflow.py # LangGraph: fetch_rss→dedup→summarize→context→primer→embed→link_entities→store
│   ├── story_workflow.py    # LangGraph: fetch_articles→extract_entities→cluster→synthesize_and_store (nested in pipeline)
│   └── compare_workflow.py  # LangGraph: search_sources→extract_and_compare→format_response
├── services/
│   ├── rss.py               # 59 RSS feeds (56 outlets) across the spectrum, feedparser, image extraction
│   ├── summarizer.py        # Claude Haiku 4.5 batch summarization
│   ├── context_generator.py # why_it_matters line + importance score
│   ├── primer_generator.py  # context_primer: background + glossary terms
│   ├── quality_gate.py      # deterministic cliché/restatement gate (#90)
│   ├── judge.py             # offline LLM judge for the quality eval (#90)
│   ├── embedder.py          # Voyage AI embeddings (voyage-3-lite, 512-dim)
│   └── deduplicator.py      # Postgres dedup check
├── tests/
├── docker-compose.yml       # Postgres 16 + pgvector (local dev)
├── init.sql                 # DB schema (9 tables; civic profile tables via migrations/)
├── Dockerfile               # Production image
├── railway.toml             # Railway deployment config
└── .github/workflows/ci.yml # Ruff + pytest on PR/push
```

## Database

Schema source of truth for a fresh database is `init.sql`. Additive changes after the initial schema are layered on via two parallel mechanisms:

| Where                              | Form                                    | Applied by                                   |
| ---------------------------------- | --------------------------------------- | -------------------------------------------- |
| `migrations/NNN_*.sql`             | `CREATE ... CONCURRENTLY IF NOT EXISTS` | Operator running `psql -f` manually          |
| `app/db.py:_apply_migrations`      | Same DDL, non-CONCURRENTLY, idempotent  | FastAPI startup (the prod path on Railway)   |

When adding a migration, write both. The SQL file is the CONCURRENTLY-safe version for live ops; the Python hook is what actually runs on every deploy.

### Feed queries and the indexes that serve them

The user-facing `/api/news` feed is served by queries that live in **`sift/lib/db.ts` in the `sift` repo**, not here. This service owns the write path and the indexes; the frontend owns the read queries. The partial indexes below (defined in `migrations/004_feed_indexes.sql` + `app/db.py:_apply_migrations`) exist to match the exact predicates those queries use.

| Query (`sift/lib/db.ts`)          | Purpose                        | Index                                          |
| --------------------------------- | ------------------------------ | ---------------------------------------------- |
| `:36`  `getArticlesByCategory`    | category fallback feed         | `idx_articles_feed`                            |
| `:85`  stories + LEFT JOIN        | top stories per category       | `idx_stories_feed` + `idx_articles_story_feed` |
| `:121` story articles             | articles belonging to a story  | `idx_articles_story_feed`                      |
| `:150` standalone articles        | articles outside any story     | `idx_articles_feed`                            |

Client abort budget is `API_TIMEOUT_MS = 10_000` in `sift/lib/constants.ts`; exceeding it surfaces as "We hit a snag pulling today's stories." If a category tab starts timing out, these indexes or these queries are the place to look.

### Diagnosing feed-query performance

```bash
python scripts/explain_feed_queries.py            # summary table
python scripts/explain_feed_queries.py --verbose  # full EXPLAIN JSON
```

Runs `EXPLAIN (ANALYZE, BUFFERS)` across all 10 categories × 3 query shapes against `DATABASE_URL`. Warns at 2000 ms, fails (exit 1) at 8000 ms.

CI wires the same script into the **`feed-perf`** job (`.github/workflows/ci.yml`), triggered only on PRs that touch `app/db.py`, `migrations/`, or the script itself. Requires a `DATABASE_URL` repo secret set to the prod Neon URL.

### Funding edges (990 Schedule I / R)

```bash
python scripts/ingest_funding_edges.py --year 2025 --dry-run   # parse + report
python scripts/ingest_funding_edges.py --year 2025 --apply     # write funding_edges
python scripts/ingest_funding_edges.py --year 2025 --dry-run --only 237327730
```

Builds edges *between* organizations — grants paid (Schedule I Part II) and
declared related tax-exempt orgs (Schedule R Part II) — for the ten curated
think tanks in `ORGS`. **Run on demand only; deliberately not on the pipeline
heartbeat**, so an empty `funding_edges` table is the normal state.

Every edge stores an `ein_name_agrees` verdict (`agrees` / `review` /
`ein_absent`) computed at ingest by `services/funding_edges.py`, which compares
the counterparty name *as filed* against the IRS's own name for that EIN. A
counterparty EIN is a join key a human typed at the filing org, and in the
first 121-edge pull one was wrong — Brookings listed "Urban League of
Louisiana" under The Urban Institute's EIN. Only `agrees` edges are
publishable; `review` means unadjudicated, not false.

Requires `stream-unzip` (in `requirements-dev.txt`, not shipped to prod): the
IRS bulk zips use Deflate64, which Python's `zipfile`, `bsdtar` and macOS
`ditto` all refuse — and `bsdtar` fails by writing a zero-byte file.

## Generation recovery

Budget/provider failures retain the RSS source in `articles.generation_raw_content`
and set `generation_status=pending`. The existing ingestion cycle retries up to
50 queued URLs, including entries no longer present in RSS. Missing source body
is `unavailable`; it is never promoted from a headline to an invented summary.
RSS fallback rows remain in `general` and skip paid enrichment. Successful retries
reset the threading marker and record `copy_rules_version`.

`GET /health` exposes the cached `generation` snapshot (`pending`, `unavailable`,
`last_completed`); pending summaries make status `degraded`. `?deep=1` refreshes
these counts explicitly. There is no new periodic database reader. Counts cover
30 days; the durable retry selector itself has no age cutoff.

Story reuse requires the current copy rules version. Recent unversioned stories
join the bounded repair sweep; a changed timestamp alone never certifies copy.
Glossary usefulness is reviewed before background novelty, and novelty review
includes retained definitions. Unavailable optional review withholds that copy.
The configured daily spending ceiling is unchanged.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Neon Postgres direct connection string |
| `ANTHROPIC_API_KEY` | Yes | Claude API key (summaries + comparison) |
| `VOYAGE_API_KEY` | Yes | Voyage AI key (embeddings) |
| `PIPELINE_API_KEY` | Yes | Shared secret for pipeline auth |
| `ENVIRONMENT` | No | `development` or `production` (enables background scheduler) |
| `PORT` | No | Server port (default: 8000, Railway injects 8080) |
| `LOG_LEVEL` | No | `debug`, `info`, `warning`, `error` |
| `SENTRY_DSN` | No | Sentry DSN for error monitoring. Inert unless set; no PII sent |
| `SENTRY_TRACES_SAMPLE_RATE` | No | Sentry tracing sample rate (default: `0.1`) |
| `AI_COST_GUARD_ENABLED` | No | Enable the daily AI cost ceiling (default: `false`) |
| `DAILY_AI_COST_LIMIT_USD` | No | Daily Claude + Voyage spend ceiling, USD (default: `10.0`) |
| `AI_COMPARE_BUDGET_SHARE` | No | Share protected from background work for interactive Compare (default: `0.2`); does not raise the daily cap |
| `AI_COST_ALERT_THRESHOLD_RATIO` | No | Budget fraction that triggers an alert (default: `0.8`) |
| `WHY_IT_MATTERS_JUDGE_ENABLED` | No | Run the runtime LLM-judge on generated `why_it_matters` lines (default: `false`; adds a paid call per kept line, increases suppression) |
| `ENTITY_LINKER_REGEX_GATE_ENABLED` | No | Send an article to the LLM entity linker only when the free regex matcher finds a candidate surface form (default: **`true`** — a kill switch, not an opt-in, since it saves rather than spends). Forwards ~26% of articles and retains 98.11% of the links the ungated path produced, measured by `scripts/eval_linker_gate.py`. Set `false` to restore one LLM call per article |
| `LLM_MODEL_OVERRIDES` | No | Per-stage model overrides against `services/model_registry.py`, as `operation=catalog_id` pairs — e.g. `summarizer.batch=sonnet-4-6,judge.batch=haiku-4-5`. Empty (the default) runs every stage on the model it used before the registry existed. A malformed entry, unknown operation, unknown model, or a model lacking a capability the stage requires is dropped with an ERROR and that stage stays on its incumbent — never a hard failure, never silent. Active overrides are logged at WARNING on startup and echoed in `GET /health` |

### Which model is a stage on?

Ask the service, don't infer it from the env var:

```bash
curl -s https://<host>/health | jq .model_overrides
```

`null` means every stage is on its default. `services/model_registry.py` holds the
mapping and `ai_usage_daily` records the model each call actually ran on, so
spend per stage per model is a SQL query rather than an assumption.

## Tests

```bash
pytest
ruff check .
```

## Deployment

Deployed to [Railway](https://railway.app). Push to `main` triggers automatic deploy. CI runs ruff + pytest on every PR via GitHub Actions.

**Production URL:** sift-api-production.up.railway.app (target port: 8080)

## Comparison provenance

The JSON compare response, SSE `results` event and newly saved daily example
include `source_provenance_version: 1`, `sources_requested`, `sources_retrieved`,
`sources_analyzed` and `sources_unavailable`. Coverage found is a search-workflow
status, not independent verification of an article or claim. `sources_checked`
is retained as an alias for successfully analyzed sources; failed searches never
fall back to the requested source list.

The formatter removes unrecognized or duplicate claim sources and calculates
agreement against the analyzed source set. `partial` means multiple sources
report a claim without a majority; a single-source claim is `unique`. Summaries
that name unavailable sources or claim an inconsistent all-source count use a
source-scoped fallback. Daily examples require at least two analyzed sources.
Older saved examples are not assigned availability metadata retroactively.

### Shared AI budget allocation

The configured daily cap is unchanged (production: $15). Background generation,
quality review, daily comparison examples and frontend topic AI stop admitting
work at 80% of that cap ($12); reader-requested JSON/SSE comparisons can use the
remaining 20% ($3) and unused background capacity. The shared PostgreSQL policy
is updated by production backend startup; frontend environment defaults cannot
silently override it. Non-production startup never replaces an existing policy.

Every production Messages call and embedding call reserves capacity atomically
before contacting the provider. Batches reserve their whole bounded payload;
their measured cost is settled before applying results, independently of handler
success. Actual usage and all unresolved holds count toward admission, including
holds from an earlier UTC date. Successful settlement is atomic and idempotent.
Definitively rejected Messages calls release their hold. Transport errors,
cancellation, missing usage, or uncertain batch submission retain it for operator
reconciliation; they must not be deleted merely to make room for more calls.
`ai_budget_holds` is a commitment ledger, not fabricated provider spend.

Search concurrency adapts to available capacity so a five-outlet request can reuse
reservations as earlier searches settle, rather than requiring five simultaneous
maximum holds when the pool is low. Atomic admission still applies to every call.

Guarded calls disable automatic provider retries. Compare uses one targeted web
search per outlet, up to five outlets, followed by the existing claim comparison.
Reservations conservatively include tool iterations, context, maximum output and
cache-write pricing. The ceiling enforces modeled API costs; provider invoices
remain the source for billing reconciliation. Unknown model/tool costs fail closed.

`GET /analyze/budget` (also under `/v1`) requires `X-Pipeline-Key` and reports the
policy version, enabled flag, configured cap/share, actual UTC spend and outstanding
holds. `/health` includes `budget_policy_version` without a database query.
Migration 036 is applied at startup before the policy is published. Inspect
unresolved holds and provider outcomes before reconciling uncertain commitments.

Local concurrency tests: set `AI_BUDGET_TEST_DATABASE_URL` to an isolated localhost
PostgreSQL database and run `pytest tests/test_budget_allocation_db.py`. CI runs
these against its disposable PostgreSQL service, with no production/provider keys.

### Compare publisher registry and retrieval

Compare accepts curated publisher names, slugs and RSS aliases from
`outlet_profiles` and `source_name_aliases`. The frontend picker reads the same
registry and offers one choice per publisher, including right-leaning outlets;
the five-outlet request limit and daily budget are unchanged.

For an exact indexed article title or completed story headline, Compare first
checks recent, completed, non-roundup reporting from that publisher. Retained
original publisher text can supply an explicitly labeled, possibly incomplete
excerpt, including descriptions shorter than 200 characters with at least 20
words. Empty, thin, headline-only and access-prompt descriptions remain search
hints. If the old record has no usable retained text, its original URL guides
live search; metadata and Sift-generated summaries never count as retrieved coverage. Broad topics and
index misses use live search. Search responses are read as provider streams and
settled from final usage through the existing budget guard. Interrupted calls
retain their uncertain-cost holds; no retry spends outside the cap.

The indexed lookup has an eight-second deadline including pool acquisition, so
a database resume does not prematurely discard stored reporting. It falls back
to search on a miss or database failure, logging the exception type without
connection details. The overall Compare request deadline remains 50 seconds.

Each claim attribution requires a supporting quotation from that outlet's
retrieved text. A deterministic check rejects invented or borrowed quotations;
stored headline/URL metadata cannot serve as the supporting passage. Agreement
counts and the summary are built from the remaining attributions. This verifies
text provenance, not factual truth or semantic entailment. It uses the same
single extraction call and token ceiling; no extra paid review call is added.

Indexed candidates are selected through the story-ID and exact-title indexes
before publisher/date filtering. Story headline rewrites retain their previous
exact headlines as aliases through migration 037's update trigger, so an
existing Compare link continues to identify the same event. Aliases never
fuzzy-match or merge stories; held stories remain excluded.

National Review searches are constrained with `allowed_domains` and
an explicit `site:` query. A known original article title is preferred to a
generated Sift headline as the query target. Scoped search results count only
when the provider supplies a substantive citation from the selected publisher;
the comparison receives those cited passages, not an uncited search summary.
If an article URL is already known, only citations for that article (including
its canonical/AMP URL identity) can supply evidence. No direct-page scraping,
access-control bypass or additional search attempt is used.

The exact-event lookup examines up to eight recent matching publisher records
so a newer metadata-only row does not hide usable reviewed original text.
Pending/held/unavailable Sift articles can supply original URL/title hints;
their generated copy and retained text never enter Compare as stored evidence.
This does not publish or release held articles. Publisher-domain aliases live
in `services/compare_search.py`; other publishers keep name-based search. Domain
adapters require live verification: Reuters rejects the provider's domain
constraint, so its working search path is preserved. Claim quotations normalize
Unicode typography while retaining word/number identity. Short excerpts may
yield one supported claim; attributed opinions remain attributed.

For a broad National Review topic with no exact indexed match, the existing
full-text index supplies up to three recent original-title/URL search hints.
They never count as retrieved reporting or change story membership. The same
single budgeted publisher search must return relevant cited passages.
Extraction selects supporting passages before writing claims and includes each
outlet's supported perspective where possible; unsupported claims stay excluded.
Discovery candidates may include reviewed original publisher excerpts. The
existing budgeted search can explicitly confirm a candidate's topic relevance
and return that original excerpt when a page/index supplies no usable text.
Unselected candidates, title-only hints and held copy never count as evidence.

Extraction assesses every retrieved outlet before grouping claims, using the
same single budgeted call and token ceiling. If grouping omits an outlet with
a verified, self-contained passage, retain that passage as an explicitly
attributed quotation. Never complete truncated text or borrow another outlet’s
evidence. Reserve room for supported outlet contributions within the existing
eight-claim ceiling. Complete short reviewed original excerpts can be retained
verbatim even when the model overlooks them; null, metadata-only, unfinished
and unsupported passages otherwise add nothing.

For an exact indexed National Review article with missing stored text, Compare
first checks the publisher’s configured public RSS feed. Match the original
article URL (including canonical/AMP identity), require a current substantive
description, and exclude the content:encoded field because NR can put only a
photo caption there. This is original publisher text, never Sift-generated copy.
The request has a two-second deadline and one-megabyte limit; redirects, access
errors, missing/stale articles and unusable descriptions fall back to the same
single budgeted search. No article-page access workaround is used. Complete
short public descriptions are quoted directly and kept out of model grouping
into factual agreement or disputes. They cannot support absent facts or inferred
context. Richer reporting retains the existing grounded extraction call.

The public RSS description gate accepts complete statements of at least six
words as literal attributed quotations; the twenty-word stored-reporting gate
remains unchanged. Slogans below that bound, incomplete/spaced ellipses, access
messages, calls to read more and headline duplicates remain excluded. Discovery
checks up to three known candidate URLs in one bounded public-feed request.
Their descriptions remain hints until the existing single budgeted search
explicitly selects a candidate for the requested subject and timing. No extra
AI call, automatic event match, generated held copy or budget increase is added.

Counterpart searches for Fox News, CBS News and NPR use publisher-site
queries without adding provider domain filters. Reuters uses a name-based query
because site-only searches exclude its republications and its provider filter
is unsupported. Broad colon-separated requests search the subject before the
colon, then verify the full requested issue against the results.
Treat another outlet’s headline as a subject cue, preserve the distinguishing
event/action and allow different headlines. An exact indexed NR article can
supply original event context solely to locate counterpart coverage; its text
never supplies counterpart claims. Accept only the selected publisher’s cited
passages, not uncited model summaries or another outlet’s quotes. Reuters has a
narrow public-republication adapter for KSL: require an exact article identity,
explicit Reuters article byline, publication within the past year, and cited
passages present in both the structured article body and visible article text.
Photo credits and ordinary mentions never prove Reuters authorship. Verification
reads at most two public pages within two seconds, with a 1 MB response limit,
no redirects or credentials, and no extra paid call. Exact indexed article
requests cannot silently substitute a republication. Keep the existing one-search
limit and event/timing relevance checks;
older or different cases and missing primary passages remain unavailable.

Reuters discovery first queries KSL's public archive for the core subject,
without the publisher keyword that dilutes archive relevance. Inspect the
first twenty metadata hits, deduplicate headlines and read at most three
canonical articles inside a three-second deadline. Only recent articles with
an explicit Reuters byline and a full structured body matching visible text
become candidates. The existing paid search-stage operation then selects a
candidate for the full requested subject/event/timing, without a web tool;
no match, titles alone and access failures add no claims. Archive failure keeps
the existing single paid web-search fallback.

When web-search admission cannot fit the remaining cap but verified indexed
originals exist, Compare can review only those originals with the same paid
stage and no web tool. Metadata-only candidates are excluded in that mode.
Atomic per-call admission and extraction accounting still apply. Budget
reservation logs now include the hold ID, operation, scope and terminal outcome
so future interrupted calls can be audited. Unknown costs are never released
solely because a reservation is old or a worker restarted.

Archive queries normalize "initial public offering" and IPO filing headlines
to the company's IPO subject; the full original topic still controls semantic
selection. Candidate fetches and parsing fail independently, retaining already
verified originals when another page is malformed or reaches the shared
deadline. This keeps the same single archive query, three-page bound and
three-second deadline. Streamed calls capture the provider request ID from the
response header so their reservation/settlement logs remain traceable.
For verbose event headlines, discovery can use an explicit compact named
subject from the headline instead of requiring every verb and detail in the
literal archive query. The complete request still governs subject, action and
timing selection; relatives, similarly named people and related cases do not
become matching coverage automatically.
