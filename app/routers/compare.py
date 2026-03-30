from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from app.models import CompareRequest, CompareResponse
from workflows.compare_workflow import build_compare_graph, CompareState

logger = logging.getLogger("sift-api.compare-router")

router = APIRouter(prefix="/analyze", tags=["compare"])

compare_graph = build_compare_graph()


@router.post("/compare", response_model=CompareResponse)
async def compare_sources(request: CompareRequest):
    if not request.topic or len(request.topic.strip()) < 3:
        raise HTTPException(status_code=400, detail="Topic must be at least 3 characters")

    if len(request.sources) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 sources allowed")

    start = time.time()

    initial_state: CompareState = {
        "topic": request.topic.strip(),
        "sources": request.sources,
        "search_results": {},
        "claims": [],
        "comparison": "",
        "errors": [],
    }

    try:
        result = await compare_graph.ainvoke(initial_state)
    except Exception as e:
        logger.error("Compare workflow failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Comparison failed: {e}")

    duration_ms = int((time.time() - start) * 1000)

    errors = result.get("errors", [])
    if errors:
        logger.warning("Compare completed with errors: %s", errors)

    comparison = result.get("comparison", "")
    claims = result.get("claims", [])

    if not comparison and not claims:
        raise HTTPException(
            status_code=502,
            detail="Could not generate comparison. The sources may not have relevant coverage.",
        )

    return CompareResponse(
        topic=request.topic,
        comparison=comparison,
        sources_checked=request.sources,
        claims=claims,
        duration_ms=duration_ms,
    )
