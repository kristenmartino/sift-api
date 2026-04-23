from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.db import init_pool, get_pool, close_pool
from app.dependencies import limiter
from app.models import HealthResponse
from app.routers import pipeline, compare

logger = logging.getLogger("sift-api")

API_VERSION = "1.0.0"

REFRESH_INTERVAL = 30 * 60  # 30 minutes (was 10 min) — stretched to cut spend 66%


async def _scheduled_refresh():
    """Run pipeline refresh every 10 minutes in production."""
    await asyncio.sleep(60)  # let the app fully start and serve initial requests
    while True:
        try:
            logger.info("Scheduled refresh starting")
            from app.routers.pipeline import pipeline as pl
            from workflows.pipeline_workflow import PipelineState
            initial_state: PipelineState = {
                "force": False,
                "articles": [],
                "new_articles": [],
                "summaries": {},
                "embeddings": {},
                "results": {},
                "total_skipped": 0,
                "errors": [],
            }
            result = await pl.ainvoke(initial_state)
            errors = result.get("errors", [])
            results = result.get("results", {})
            logger.info("Scheduled refresh done: %s categories, %d errors", len(results), len(errors))
        except Exception as e:
            logger.error("Scheduled refresh failed: %s", e)
        await asyncio.sleep(REFRESH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.info("Starting sift-api (env=%s)", settings.environment)
    if settings.pipeline_api_key in ("dev-key", "change-me-in-production", ""):
        logger.warning(
            "SECURITY: PIPELINE_API_KEY is set to a default/empty value. "
            "Set a strong, unique key via the PIPELINE_API_KEY environment variable."
        )
    try:
        await init_pool()
        logger.info("Database pool initialized")
    except Exception as e:
        logger.warning("Failed to connect to database: %s", e)

    # Start background scheduler in production
    cron_task = None
    poller_task = None
    if settings.environment == "production":
        cron_task = asyncio.create_task(_scheduled_refresh())
        logger.info("Scheduled refresh enabled (every %ds)", REFRESH_INTERVAL)

        # Phase 6: poll Anthropic Message Batches for completion and apply
        # results. Runs in prod only; dev uses sync API for quick iteration.
        from services.batch_poller import run_batch_poller
        poller_task = asyncio.create_task(run_batch_poller())

    yield

    # Shutdown
    if cron_task:
        cron_task.cancel()
    if poller_task:
        poller_task.cancel()
    await close_pool()
    logger.info("sift-api shut down")


app = FastAPI(
    title="Sift API",
    version=API_VERSION,
    description=(
        "AI-curated news pipeline and multi-source comparison API for Sift. "
        "Handles background content processing (RSS feeds, Claude summaries, "
        "Voyage AI embeddings) and on-demand multi-source news comparison."
    ),
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."},
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Generate or echo request ID for tracing
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Permissions-Policy"] = "()"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Language"] = "en"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://siftnews.ai",
        "https://www.siftnews.ai",
        "https://siftnews.kristenmartino.ai",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Pipeline-Key"],
)

# Versioned routes (preferred)
app.include_router(pipeline.router, prefix="/v1")
app.include_router(compare.router, prefix="/v1")

# Legacy routes (backwards-compatible, migrate frontend then remove)
app.include_router(pipeline.router)
app.include_router(compare.router)


@app.get(
    "/",
    summary="Service info",
    description="Returns service metadata and available API endpoints.",
)
async def root():
    return {
        "service": "sift-api",
        "version": API_VERSION,
        "endpoints": {
            "health": "GET /health",
            "pipeline": "POST /v1/pipeline/refresh",
            "compare": "POST /v1/analyze/compare",
            "docs": "GET /docs",
            "redoc": "GET /redoc",
        },
    }


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns service health, database connectivity, and last pipeline run timestamp.",
)
async def health():
    db_connected = False
    last_run = None
    try:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
        db_connected = True
        row = await pool.fetchrow(
            "SELECT MAX(last_refreshed_at) as last_run FROM pipeline_state"
        )
        if row and row["last_run"]:
            last_run = row["last_run"].isoformat()
    except Exception:
        pass

    scheduler_running = (
        settings.environment == "production" or None
    )

    return HealthResponse(
        status="healthy" if db_connected else "degraded",
        version=API_VERSION,
        db_connected=db_connected,
        last_pipeline_run=last_run,
        scheduler_running=scheduler_running if scheduler_running else None,
    )
