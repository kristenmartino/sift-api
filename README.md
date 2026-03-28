# sift-api

Python FastAPI + LangGraph backend for [Sift](https://siftnews.ai) — the AI news reader.

Handles the background content pipeline: RSS feeds → Claude Haiku summaries → Voyage AI embeddings → Postgres. Also serves the multi-source comparison workflow.

## Architecture

```
Vercel Cron → POST /pipeline/refresh → LangGraph pipeline:
  fetch_rss → deduplicate → summarize (Claude) → embed (Voyage) → store (Postgres)
```

User-facing reads happen in the Next.js frontend — this service only handles background AI processing.

## Setup

### Prerequisites

- Python 3.9+ (targeting 3.12)
- Docker (for Postgres + pgvector)

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
```

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check + DB status + last pipeline run |
| POST | `/pipeline/refresh` | Trigger RSS pipeline (auth required) |
| POST | `/analyze/compare` | Multi-source comparison (not yet implemented) |

## Project structure

```
sift-api/
├── app/
│   ├── main.py              # FastAPI app, health endpoint
│   ├── config.py            # pydantic-settings
│   ├── db.py                # asyncpg connection pool
│   ├── models.py            # Pydantic schemas
│   └── routers/
│       ├── pipeline.py      # POST /pipeline/refresh
│       └── compare.py       # POST /analyze/compare (stub)
├── workflows/
│   ├── pipeline_workflow.py # LangGraph: fetch→dedup→summarize→embed→store
│   └── compare_workflow.py  # Stub
├── services/
│   ├── rss.py               # 28 RSS feeds, feedparser, image extraction
│   ├── summarizer.py        # Claude Haiku batch summarization
│   ├── embedder.py          # Voyage AI embeddings
│   └── deduplicator.py      # Postgres dedup check
├── tests/
├── docker-compose.yml       # Postgres 16 + pgvector
├── init.sql                 # DB schema (4 tables)
├── Dockerfile               # Production image
└── railway.toml             # Railway deployment config
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string |
| `ANTHROPIC_API_KEY` | Yes | Claude API key for summaries |
| `VOYAGE_API_KEY` | Yes | Voyage AI key for embeddings |
| `PIPELINE_API_KEY` | Yes | Shared secret for pipeline auth |
| `PORT` | No | Server port (default: 8000) |
| `ENVIRONMENT` | No | `development` or `production` |
| `LOG_LEVEL` | No | `debug`, `info`, `warning`, `error` |

## Tests

```bash
pytest
```

## Deployment

Deployed to [Railway](https://railway.app). Push to `main` triggers automatic deploy.

```bash
# Manual Docker build
docker build -t sift-api .
docker run -p 8000:8000 --env-file .env sift-api
```
