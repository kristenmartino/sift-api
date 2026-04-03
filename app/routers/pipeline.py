from __future__ import annotations

import hmac
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.dependencies import limiter
from app.models import PipelineRequest, PipelineResponse
from workflows.pipeline_workflow import build_pipeline_graph, PipelineState

logger = logging.getLogger("sift-api.pipeline-router")

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

pipeline = build_pipeline_graph()


@router.post(
    "/refresh",
    response_model=PipelineResponse,
    summary="Trigger RSS pipeline",
    description=(
        "Triggers the full content pipeline: fetch RSS feeds, deduplicate, "
        "summarize with Claude, generate embeddings, and store in Postgres. "
        "Rate limited to 5 requests per minute."
    ),
)
@limiter.limit("5/minute")
async def refresh_pipeline(
    request: Request,
    body: PipelineRequest,
    x_pipeline_key: str = Header(...),
):
    if not hmac.compare_digest(x_pipeline_key, settings.pipeline_api_key):
        raise HTTPException(status_code=401, detail="Invalid pipeline key")

    start = time.time()

    initial_state: PipelineState = {
        "force": body.force,
        "articles": [],
        "new_articles": [],
        "summaries": {},
        "embeddings": {},
        "results": {},
        "total_skipped": 0,
        "errors": [],
    }

    try:
        result = await pipeline.ainvoke(initial_state)
    except Exception as e:
        logger.error("Pipeline failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"detail": "Pipeline execution failed", "code": "PIPELINE_FAILED"},
        )

    duration_ms = int((time.time() - start) * 1000)

    errors = result.get("errors", [])
    if errors:
        logger.warning("Pipeline completed with errors: %s", errors)

    return PipelineResponse(
        results=result.get("results", {}),
        total_skipped=result.get("total_skipped", 0),
        duration_ms=duration_ms,
    )
