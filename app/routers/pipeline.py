from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Header, HTTPException

from app.config import settings
from app.models import PipelineRequest, PipelineResponse
from workflows.pipeline_workflow import build_pipeline_graph, PipelineState

logger = logging.getLogger("sift-api.pipeline-router")

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

pipeline = build_pipeline_graph()


@router.post("/refresh", response_model=PipelineResponse)
async def refresh_pipeline(
    request: PipelineRequest,
    x_pipeline_key: str = Header(...),
):
    if x_pipeline_key != settings.pipeline_api_key:
        raise HTTPException(status_code=401, detail="Invalid pipeline key")

    start = time.time()

    initial_state: PipelineState = {
        "force": request.force,
        "articles": [],
        "new_articles": [],
        "summaries": {},
        "embeddings": {},
        "results": {},
        "errors": [],
    }

    try:
        result = await pipeline.ainvoke(initial_state)
    except Exception as e:
        logger.error("Pipeline failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Pipeline execution failed: {e}")

    duration_ms = int((time.time() - start) * 1000)

    errors = result.get("errors", [])
    if errors:
        logger.warning("Pipeline completed with errors: %s", errors)

    return PipelineResponse(
        results=result.get("results", {}),
        duration_ms=duration_ms,
    )
