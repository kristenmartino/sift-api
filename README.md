# sift-api

Python FastAPI + LangGraph backend for [Sift](https://siftnews.kristenmartino.ai) — the AI-curated news reader.

Handles the background content pipeline (RSS feeds → Claude Haiku summaries → Voyage AI embeddings → Neon Postgres) and the multi-source comparison workflow (LangGraph fan-out web search → claim extraction → comparison).

## Architecture

```
Railway asyncio scheduler (every 30 min)
  → LangGraph pipeline: fetch_rss → deduplicate → summarize (Claude) → embed (Voyage) → store (Postgres)

User compare request (via Vercel proxy)
  → LangGraph compare: search_sources (parallel) → extract_and_compare → format_response
```

User-facing reads happen in the Next.js frontend — this service handles background AI processing and on-demand comparison.

## Setup

### Prerequisites

- Python 3.12+
- Docker (for local Postgres + pgvector)

### Local development

```bash
# Start Postgres
docker compose up -d

# Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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
| GET | `/health` | Health check + DB status + last pipeline run |
| GET | `/docs` | Interactive API documentation (Swagger UI) |
| GET | `/redoc` | Alternative API documentation (ReDoc) |
| POST | `/v1/pipeline/refresh` | Trigger RSS pipeline (auth required) |
| POST | `/v1/analyze/compare` | Multi-source comparison via LangGraph |

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
│   ├── pipeline_workflow.py # LangGraph: fetch→dedup→summarize→embed→store
│   └── compare_workflow.py  # LangGraph: search→extract→compare→format
├── services/
│   ├── rss.py               # 100+ RSS feeds, feedparser, image extraction
│   ├── summarizer.py        # Claude Haiku 4.5 batch summarization
│   ├── embedder.py          # Voyage AI embeddings (voyage-3-lite, 512-dim)
│   └── deduplicator.py      # Postgres dedup check
├── tests/
├── docker-compose.yml       # Postgres 16 + pgvector (local dev)
├── init.sql                 # DB schema (4 tables)
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
| `AI_COST_ALERT_THRESHOLD_RATIO` | No | Budget fraction that triggers an alert (default: `0.8`) |

## Tests

```bash
pytest
ruff check .
```

## Deployment

Deployed to [Railway](https://railway.app). Push to `main` triggers automatic deploy. CI runs ruff + pytest on every PR via GitHub Actions.

**Production URL:** sift-api-production.up.railway.app (target port: 8080)
