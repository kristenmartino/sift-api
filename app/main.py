from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import init_pool, get_pool, close_pool
from app.models import HealthResponse
from app.routers import pipeline, compare

logger = logging.getLogger("sift-api")

REFRESH_INTERVAL = 10 * 60  # 10 minutes


async def _scheduled_refresh():
    """Run pipeline refresh every 10 minutes in production."""
    await asyncio.sleep(30)  # let the app fully start
    while True:
        try:
            logger.info("Scheduled refresh starting")
            from app.routers.pipeline import pipeline as pl
            result = await pl.ainvoke({"force": False})
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
    try:
        await init_pool()
        logger.info("Database pool initialized")
    except Exception as e:
        logger.warning("Failed to connect to database: %s", e)

    # Start background scheduler in production
    cron_task = None
    if settings.environment == "production":
        cron_task = asyncio.create_task(_scheduled_refresh())
        logger.info("Scheduled refresh enabled (every %ds)", REFRESH_INTERVAL)

    yield

    # Shutdown
    if cron_task:
        cron_task.cancel()
    await close_pool()
    logger.info("sift-api shut down")


app = FastAPI(
    title="Sift API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://siftnews.ai",
        "https://www.siftnews.ai",
        "https://siftnews.kristenmartino.ai",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pipeline.router)
app.include_router(compare.router)


@app.get("/")
async def root():
    return {
        "service": "sift-api",
        "version": "0.1.0",
        "endpoints": {
            "health": "GET /health",
            "pipeline": "POST /pipeline/refresh",
            "compare": "POST /analyze/compare",
        },
    }


@app.get("/health", response_model=HealthResponse)
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
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        db_connected=db_connected,
        last_pipeline_run=last_run,
    )
