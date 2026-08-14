from __future__ import annotations

import asyncio
import logging
import time
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
from app.observability import init_sentry
from app.routers import pipeline, compare

logger = logging.getLogger("sift-api")

API_VERSION = "1.0.0"

REFRESH_INTERVAL = 30 * 60  # 30 minutes (was 10 min) — stretched to cut spend 66%


async def _scheduled_refresh():
    """Run the pipeline on a fixed interval (REFRESH_INTERVAL, 30 min) in production."""
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
            # Time the run. The HTTP path has always reported duration_ms
            # (app/routers/pipeline.py:64) but the scheduled path — the one
            # that actually runs 48x/day — did not, so how long a cycle takes
            # was recorded nowhere. It is the input that decides how much
            # Neon compute time scale-to-zero can actually save: the compute
            # stays awake for the run plus a 300s idle tail, so a 2-minute
            # cycle and an 8-minute cycle are a 2x difference in the bill.
            start = time.monotonic()
            result = await pl.ainvoke(initial_state)
            duration_s = time.monotonic() - start
            errors = result.get("errors", [])
            results = result.get("results", {})
            logger.info(
                "Scheduled refresh done: %s categories, %d errors, %.1fs",
                len(results), len(errors), duration_s,
            )
            # Reconcile the batch poller's in-memory in-flight set against
            # api_batches, once per cycle. Free — the run above already woke
            # the compute — and it is the only thing standing between us and a
            # batch submitted by some future out-of-process writer sitting
            # unclaimed until the next restart. Deliberately not on its own
            # timer; a timer here is the defect the poller change removed.
            try:
                from services import batch_client
                await batch_client.sync_pending_from_db()
            except Exception as e:
                logger.warning("Batch reconcile failed: %s", e)
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

    # Name any stage not on its incumbent model, at WARNING so it stands out in
    # the log. A model override is a deliberate, temporary deviation and every
    # cost or quality number taken while one is live has to be read knowing
    # that — the alternative is discovering it while explaining a metric.
    from services.model_registry import non_default_assignments

    overrides = non_default_assignments()
    if overrides:
        logger.warning(
            "LLM model overrides active: %s",
            ", ".join(f"{op}={cid}" for op, cid in sorted(overrides.items())),
        )
    else:
        logger.info("LLM models: all stages on their incumbent")
    from app import pipeline_clock

    try:
        await init_pool()
        logger.info("Database pool initialized")
        # Seed the in-memory pipeline clock from the one authoritative read we
        # still make. Piggybacks on the awake window _apply_migrations just
        # created, so it costs no additional wake. After this, /health never
        # touches the database — see app/pipeline_clock.py.
        try:
            pool = await get_pool()
            row = await pool.fetchrow(
                "SELECT MAX(last_refreshed_at) AS last_run FROM pipeline_state"
            )
            pipeline_clock.seed(row["last_run"] if row else None, db_ok=True)
        except Exception as e:
            logger.warning("Could not seed pipeline clock: %s", e)
            pipeline_clock.seed(None, db_ok=False)
    except Exception as e:
        logger.warning("Failed to connect to database: %s", e)
        pipeline_clock.seed(None, db_ok=False)

    # Start background scheduler in production
    cron_task = None
    poller_task = None
    feed_health_task = None
    feed_balance_task = None
    if settings.environment == "production":
        cron_task = asyncio.create_task(_scheduled_refresh())
        logger.info("Scheduled refresh enabled (every %ds)", REFRESH_INTERVAL)

        # Phase 6: poll Anthropic Message Batches for completion and apply
        # results. Runs in prod only; dev uses sync API for quick iteration.
        from services.batch_poller import run_batch_poller
        poller_task = asyncio.create_task(run_batch_poller())

        # #125: notice when a configured outlet stops producing. Daily, not
        # per-run — it is a question about days, and rss.py's per-run
        # feed_stats cannot answer it (that counts articles fetched, and a
        # frozen feed keeps serving the same ones).
        from services.feed_health import run_feed_health_monitor
        feed_health_task = asyncio.create_task(run_feed_health_monitor())

        # Ranking v2 stage 3: feed-balance drift tripwire. Daily snapshot of
        # the ranked pool's grim share + civic density per category, tripping
        # on drift vs a trailing baseline — the D48/D45 policy numbers as
        # logged metrics instead of vibes.
        from services.feed_balance import run_feed_balance_monitor
        feed_balance_task = asyncio.create_task(run_feed_balance_monitor())

    yield

    # Shutdown
    if cron_task:
        cron_task.cancel()
    if poller_task:
        poller_task.cancel()
    if feed_health_task:
        feed_health_task.cancel()
    if feed_balance_task:
        feed_balance_task.cancel()
    await close_pool()
    logger.info("sift-api shut down")


# Initialize error monitoring before the app is created so Sentry's ASGI
# integration wraps request handling. No-op unless SENTRY_DSN is configured.
init_sentry()


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
        "https://siftnews.io",
        "https://www.siftnews.io",
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
    description=(
        "Returns service health, database connectivity, and last pipeline run "
        "timestamp. Makes no database query — the values are held in memory by "
        "app/pipeline_clock.py. Pass ?deep=1 for a live connectivity probe, "
        "which will wake a suspended Neon compute."
    ),
)
async def health(deep: bool = False):
    from app import pipeline_clock

    db_connected = pipeline_clock.db_ok()

    if deep:
        # Opt-in only. Nothing on a schedule requests it: the GitHub heartbeat
        # reads .last_pipeline_run, and Railway's deploy healthcheck cannot
        # pass query parameters.
        try:
            pool = await get_pool()
            await pool.fetchval("SELECT 1")
            db_connected = True
        except Exception:
            db_connected = False
            pipeline_clock.note_db_error()

    run_at = pipeline_clock.last_pipeline_run()
    last_run = run_at.isoformat() if run_at else None

    scheduler_running = (
        settings.environment == "production" or None
    )

    from services.model_registry import non_default_assignments

    overrides = non_default_assignments()

    return HealthResponse(
        status="healthy" if db_connected else "degraded",
        version=API_VERSION,
        db_connected=db_connected,
        last_pipeline_run=last_run,
        scheduler_running=scheduler_running if scheduler_running else None,
        model_overrides=overrides or None,
    )
