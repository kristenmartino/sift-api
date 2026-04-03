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
    db_connected: bool
    last_pipeline_run: str | None
    scheduler_running: bool | None = None


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
