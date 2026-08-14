from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


# --- Pipeline ---

class PipelineRequest(BaseModel):
    force: bool = False


class CategoryResult(BaseModel):
    new_articles: int = 0
    skipped: int = 0
    errors: int = 0


class PipelineResponse(BaseModel):
    results: dict[str, CategoryResult]
    total_skipped: int = 0
    duration_ms: int


# --- Comparison ---

class Claim(BaseModel):
    claim: str
    agreement: str  # "unanimous", "majority", "disputed", "unique"
    sources: list[str] = []
    sources_for: list[str] = []
    sources_against: list[str] = []


class CompareRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=500)
    sources: list[str] = Field(
        default=["reuters", "bbc", "associated press"],
        max_length=5,
    )


class CompareResponse(BaseModel):
    topic: str
    comparison: str
    sources_checked: list[str]
    claims: list[Claim]
    duration_ms: int


# --- Stories ---

class StoryFraming(BaseModel):
    source_name: str
    framing: str
    tone: str


class EntitySet(BaseModel):
    people: list[str] = []
    organizations: list[str] = []
    locations: list[str] = []
    event_description: str = ""


# --- Health ---

class HealthResponse(BaseModel):
    status: str
    version: str
    # "The last database operation this process attempted succeeded" — NOT a
    # live probe. /health stopped querying Postgres so that a 30-minute
    # heartbeat could not hold Neon's scale-to-zero timer open indefinitely
    # (see app/pipeline_clock.py); the value is seeded at startup and refreshed
    # by each pipeline run. This is a weaker claim than it used to be, and is
    # written down here rather than left to be discovered. GET /health?deep=1
    # still does the real SELECT 1 when a live answer is what you want.
    db_connected: bool
    # Also served from memory. Goes stale exactly when the pipeline stops,
    # which is what the heartbeat's staleness threshold is watching for.
    last_pipeline_run: str | None
    scheduler_running: bool | None = None
    # operation -> wire model id. Present only when LLM_MODEL_OVERRIDES has
    # moved something off its incumbent, so the normal payload is unchanged and
    # a non-empty value is itself the signal. Without this, "which model is
    # this stage actually on" can only be inferred from the env var, and
    # CLAUDE.md's rule about deploys applies just as well here: read it, do not
    # infer it.
    model_overrides: dict[str, str] | None = None


# --- Errors ---

class ErrorResponse(BaseModel):
    detail: str
    code: str


# --- Internal: RSS article before summarization ---

class RSSArticle(BaseModel):
    title: str
    source_url: str
    source_name: str
    published_date: datetime | None = None
    image_url: str | None = None
    category: str = ""  # Empty until AI classifies during summarization
    raw_content: str = ""  # RSS description/content, used for summarization input
    content_hash: str | None = None  # sha256(norm_title + norm_content[:500]), for dedup
