from __future__ import annotations

import hmac
import json
import logging
import time

import asyncio

import anthropic
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.dependencies import limiter
from app.models import CompareRequest, CompareResponse
from services.cost_estimates import estimate_cost
from services.cost_guard import check_budget, budget_snapshot
from services.paid_calls import BudgetUnavailable, budget_scope
from services.daily_compare import refresh_daily_example
from workflows.compare_workflow import (
    CompareState,
    comparison_provenance,
    build_compare_graph,
    extract_and_compare_node,
    filter_allowed_sources,
    format_response_node,
    load_allowed_sources,
    search_one_source,
    source_found_coverage,
)

logger = logging.getLogger("sift-api.compare-router")

router = APIRouter(prefix="/analyze", tags=["compare"])

# Overall compare-workflow ceiling. Kept within the frontend proxy budget
# (sift#122): backend 50s < proxy abort 55s < Vercel maxDuration 60s < client 65s.
COMPARE_TIMEOUT = 50  # seconds


compare_graph = build_compare_graph()


@router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Multi-source news comparison",
    description=(
        "Searches multiple news sources for coverage of a topic, extracts key claims, "
        "and compares how sources agree or disagree. Supports up to 5 sources. "
        "Rate limited to 10 requests per minute. May take up to 50 seconds."
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

    # Reader comparisons can use the protected share and unused background
    # headroom. Each provider call also obtains an atomic shared reservation.
    budget = await check_budget(
        estimate_cost("compare.search_sources", len(body.sources)), scope="compare"
    )
    if not budget.allowed:
        logger.warning(
            "Compare blocked by cost guard (reason=%s, spent=$%.4f / $%.2f)",
            budget.reason,
            budget.spent_usd,
            budget.limit_usd,
        )
        if budget.reason == "guard_unavailable":
            # Fail-closed: we couldn't verify today's spend, so we don't make the
            # paid provider call.
            detail = (
                "Comparison is temporarily unavailable: the cost guard could "
                "not verify the AI budget. Please try again shortly."
            )
            code = "COST_GUARD_UNAVAILABLE"
        else:
            detail = (
                "Comparison is temporarily unavailable: today's AI budget has "
                "been reached. Please try again tomorrow."
            )
            code = "AI_BUDGET_EXCEEDED"
        raise HTTPException(status_code=503, detail={"detail": detail, "code": code})

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

    token = budget_scope.set("compare")
    try:
        result = await asyncio.wait_for(
            compare_graph.ainvoke(initial_state),
            timeout=COMPARE_TIMEOUT,
        )
    except BudgetUnavailable as e:
        raise HTTPException(status_code=503, detail={"detail": "Comparison capacity is temporarily unavailable. Please try again later.", "code": e.code}) from e
    except asyncio.TimeoutError as e:
        logger.error("Compare workflow timed out after %ds", COMPARE_TIMEOUT)
        raise HTTPException(
            status_code=504,
            detail={"detail": "Comparison timed out. Try fewer sources or a simpler topic.", "code": "COMPARISON_TIMEOUT"},
        ) from e
    except Exception as e:
        logger.error("Compare workflow failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"detail": "Comparison failed", "code": "COMPARISON_FAILED"},
        ) from e

    finally:
        budget_scope.reset(token)

    duration_ms = int((time.time() - start) * 1000)

    errors = result.get("errors", [])
    if errors:
        logger.warning("Compare completed with errors: %s", errors)

    comparison = result.get("comparison", "")
    claims = result.get("claims", [])
    provenance = comparison_provenance({**initial_state, **result})

    if (not comparison and not claims) or not provenance["sources_analyzed"]:
        raise HTTPException(
            status_code=502,
            detail={
                "detail": "Could not generate comparison. The sources may not have relevant coverage.",
                "code": "NO_COVERAGE",
            },
        )

    return CompareResponse(
        topic=sanitized_topic,
        comparison=comparison,
        sources_checked=provenance["sources_analyzed"],
        claims=claims,
        duration_ms=duration_ms,
        **provenance,
    )


def _sse(event: str, data: dict) -> str:
    """One SSE frame — same wire format as sift's topic route (sseEvent)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post(
    "/compare/stream",
    summary="Multi-source news comparison (SSE)",
    description=(
        "The same comparison as POST /compare, streamed as Server-Sent Events: "
        "`stage` on each pipeline phase, `source-done` as each outlet's search "
        "finishes, then `results` with the full CompareResponse payload, or "
        "`error`. Rate limited to 10 requests per minute."
    ),
)
@limiter.limit("10/minute")
async def compare_sources_stream(
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

    # Same fail-closed budget pre-check as the JSON endpoint. Raising here
    # (before the stream opens) lets the proxy surface a normal HTTP error.
    budget = await check_budget(
        estimate_cost("compare.search_sources", len(body.sources)), scope="compare"
    )
    if not budget.allowed:
        if budget.reason == "guard_unavailable":
            raise HTTPException(
                status_code=503,
                detail={
                    "detail": (
                        "Comparison is temporarily unavailable: the cost guard "
                        "could not verify the AI budget. Please try again shortly."
                    ),
                    "code": "COST_GUARD_UNAVAILABLE",
                },
            )
        raise HTTPException(
            status_code=503,
            detail={
                "detail": (
                    "Comparison is temporarily unavailable: today's AI budget "
                    "has been reached. Please try again tomorrow."
                ),
                "code": "AI_BUDGET_EXCEEDED",
            },
        )

    sanitized_topic = body.topic.strip()
    seen: set[str] = set()
    unique_sources: list[str] = []
    for s in body.sources:
        key = s.lower().strip()
        if key not in seen:
            seen.add(key)
            unique_sources.append(s)

    async def event_stream():
        """The same three stages the LangGraph runs, reported as they happen.

        Search fans out with as_completed so each outlet's finish is an event;
        extract/format reuse the graph's own node functions, so the two
        endpoints cannot drift apart in behavior — only in reporting.
        """
        start = time.time()
        token = budget_scope.set("compare")
        tasks: list[asyncio.Task] = []
        try:
            async with asyncio.timeout(COMPARE_TIMEOUT):
                allowed = await load_allowed_sources()
                sources = filter_allowed_sources(unique_sources, allowed)
                if not sources:
                    yield _sse("error", {"message": "No valid sources provided", "code": "NO_VALID_SOURCES"})
                    return

                yield _sse("stage", {"stage": "search", "sources": sources})

                client = anthropic.AsyncAnthropic(
                    api_key=settings.anthropic_api_key, max_retries=2
                )
                tasks = [
                    asyncio.create_task(search_one_source(client, sanitized_topic, s))
                    for s in sources
                ]
                search_results: dict[str, str] = {}
                for finished in asyncio.as_completed(tasks):
                    source, text = await finished
                    found = source_found_coverage(text)
                    if found and text:
                        search_results[source] = text
                    yield _sse("source-done", {"source": source, "found": found})

                if not search_results:
                    yield _sse(
                        "error",
                        {
                            "message": "Could not generate comparison. The sources may not have relevant coverage.",
                            "code": "NO_COVERAGE",
                        },
                    )
                    return

                yield _sse("stage", {"stage": "claims"})
                state: CompareState = {
                    "topic": sanitized_topic,
                    "sources": unique_sources,
                    "search_results": search_results,
                    "claims": [],
                    "comparison": "",
                    "errors": [],
                }
                extracted = await extract_and_compare_node(state)

                yield _sse("stage", {"stage": "summary"})
                state["claims"] = extracted.get("claims", [])
                state["comparison"] = extracted.get("comparison", "")
                state["analyzed_sources"] = extracted.get("analyzed_sources", [])
                formatted = await format_response_node(state)

                comparison = formatted.get("comparison", "")
                claims = formatted.get("claims", [])
                if not comparison and not claims:
                    yield _sse(
                        "error",
                        {
                            "message": "Could not generate comparison. The sources may not have relevant coverage.",
                            "code": "NO_COVERAGE",
                        },
                    )
                    return

                yield _sse(
                    "results",
                    {
                        "topic": sanitized_topic,
                        "comparison": comparison,
                        "sources_checked": comparison_provenance(state)["sources_analyzed"],
                        **comparison_provenance(state),
                        "claims": claims,
                        "duration_ms": int((time.time() - start) * 1000),
                    },
                )
        except BudgetUnavailable as e:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            yield _sse("error", {"message": "Comparison capacity is temporarily unavailable. Please try again later.", "code": e.code})
        except TimeoutError:
            logger.error("Compare stream timed out after %ds", COMPARE_TIMEOUT)
            yield _sse(
                "error",
                {
                    "message": "Comparison timed out. Try fewer sources or a simpler topic.",
                    "code": "COMPARISON_TIMEOUT",
                },
            )
        except Exception as e:
            logger.error("Compare stream failed: %s", e)
            yield _sse("error", {"message": "Comparison failed", "code": "COMPARISON_FAILED"})
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            budget_scope.reset(token)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx-style proxies not to buffer the stream.
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/compare/daily/refresh",
    summary="Force-refresh the daily compare example",
    description=(
        "Regenerates the anonymous daily compare example immediately instead "
        "of waiting for the next pipeline run. For first-deploy seeding and "
        "ops; the payload is read by the frontend from Postgres directly."
    ),
)
@limiter.limit("5/minute")
async def refresh_daily(
    request: Request,
    x_pipeline_key: str = Header(...),
):
    if not hmac.compare_digest(x_pipeline_key, settings.pipeline_api_key):
        raise HTTPException(status_code=401, detail="Invalid pipeline key")
    refreshed = await refresh_daily_example(force=True)
    return {"refreshed": refreshed}


@router.get("/budget")
async def compare_budget(x_pipeline_key: str = Header(...)):
    if not hmac.compare_digest(x_pipeline_key, settings.pipeline_api_key):
        raise HTTPException(status_code=401, detail="Invalid pipeline key")
    return await budget_snapshot()
