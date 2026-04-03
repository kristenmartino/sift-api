from __future__ import annotations

import hmac
import logging
import time

import asyncio

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.dependencies import limiter
from app.models import CompareRequest, CompareResponse
from workflows.compare_workflow import build_compare_graph, CompareState

logger = logging.getLogger("sift-api.compare-router")

router = APIRouter(prefix="/analyze", tags=["compare"])

COMPARE_TIMEOUT = 90  # seconds — max time for entire comparison workflow

compare_graph = build_compare_graph()


@router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Multi-source news comparison",
    description=(
        "Searches multiple news sources for coverage of a topic, extracts key claims, "
        "and compares how sources agree or disagree. Supports up to 5 sources. "
        "Rate limited to 10 requests per minute. May take up to 90 seconds."
    ),
)
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
        result = await asyncio.wait_for(
            compare_graph.ainvoke(initial_state),
            timeout=COMPARE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("Compare workflow timed out after %ds", COMPARE_TIMEOUT)
        raise HTTPException(
            status_code=504,
            detail={"detail": "Comparison timed out. Try fewer sources or a simpler topic.", "code": "COMPARISON_TIMEOUT"},
        )
    except Exception as e:
        logger.error("Compare workflow failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"detail": "Comparison failed", "code": "COMPARISON_FAILED"},
        )

    duration_ms = int((time.time() - start) * 1000)

    errors = result.get("errors", [])
    if errors:
        logger.warning("Compare completed with errors: %s", errors)

    comparison = result.get("comparison", "")
    claims = result.get("claims", [])

    if not comparison and not claims:
        raise HTTPException(
            status_code=502,
            detail={
                "detail": "Could not generate comparison. The sources may not have relevant coverage.",
                "code": "NO_COVERAGE",
            },
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
