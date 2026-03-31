# sift-api

Python FastAPI + LangGraph backend for [Sift](https://siftnews.kristenmartino.ai) — the AI-curated news reader.

Handles the background content pipeline (RSS feeds → Claude Haiku summaries → Voyage AI embeddings → Neon Postgres) and the multi-source comparison workflow (LangGraph fan-out web search → claim extraction → comparison).

## Architecture

```
Railway asyncio scheduler (every 10 min)
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

# Trigger pipeline (technology category)
curl -X POST http://localhost:8000/pipeline/refresh \
  -H "Content-Type: application/json" \
  -H "X-Pipeline-Key: dev-key" \
  -d '{"categories": ["technology"]}'

# Multi-source comparison
curl -X POST http://localhost:8000/analyze/compare \
  -H "Content-Type: application/json" \
  -d '{"topic": "Federal Reserve interest rate decision", "sources": ["reuters", "bbc", "associated press"]}'
```

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Service info + available endpoints |
| GET | `/health` | Health check + DB status + last pipeline run |
| POST | `/pipeline/refresh` | Trigger RSS pipeline (auth required) |
| POST | `/analyze/compare` | Multi-source comparison via LangGraph |

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
│   ├── embedder.py          # Voyage AI embeddings (voyage-3-lite, 1024-dim)
│   └── deduplicator.py      # Postgres dedup check
├── tests/
├── docker-compose.yml       # Postgres 16 + pgvector (local dev)
├── init.sql                 # DB schema (4 tables)
├── Dockerfile               # Production image
├── railway.toml             # Railway deployment config
└── .github/workflows/ci.yml # Ruff + pytest on PR/push
```

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

## Tests

```bash
pytest
ruff check .
```

## Deployment

Deployed to [Railway](https://railway.app). Push to `main` triggers automatic deploy. CI runs ruff + pytest on every PR via GitHub Actions.

**Production URL:** sift-api-production.up.railway.app (target port: 8080)
