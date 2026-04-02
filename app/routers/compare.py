from __future__ import annotations

import hmac
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.models import CompareRequest, CompareResponse
from workflows.compare_workflow import build_compare_graph, CompareState

logger = logging.getLogger("sift-api.compare-router")

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/analyze", tags=["compare"])

compare_graph = build_compare_graph()


@router.post("/compare", response_model=CompareResponse)
@limiter.limit("10/minute")
async def compare_sources(
    request: Request,
    body: CompareRequest,
    x_pipeline_key: str = Header(...),
):
    if not hmac.compare_digest(x_pipeline_key, settings.pipeline_api_key):
        raise HTTPException(status_code=401, detail="Invalid pipeline key")

    if not body.topic or len(body.topic.strip()) < 3:
        raise HTTPException(status_code=400, detail="Topic must be at least 3 characters")

    if len(body.sources) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 sources allowed")

    start = time.time()

    # Deduplicate sources while preserving order
    seen: set[str] = set()
    unique_sources: list[str] = []
    for s in body.sources:
        key = s.lower().strip()
        if key not in seen:
            seen.add(key)
            unique_sources.append(s)

    sanitized_topic = body.topic.strip()

    initial_state: CompareState = {
        "topic": sanitized_topic,
        "sources": unique_sources,
        "search_results": {},
        "claims": [],
        "comparison": "",
        "errors": [],
    }

    try:
        result = await compare_graph.ainvoke(initial_state)
    except Exception as e:
        logger.error("Compare workflow failed: %s", e)
        raise HTTPException(status_code=500, detail="Comparison failed")

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

    # Report only the sources that were actually searched (present in search_results)
    actually_checked = list(result.get("search_results", {}).keys()) or unique_sources

    return CompareResponse(
        topic=sanitized_topic,
        comparison=comparison,
        sources_checked=actually_checked,
        claims=claims,
        duration_ms=duration_ms,
    )
