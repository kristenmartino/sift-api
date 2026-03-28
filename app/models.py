from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


# --- Pipeline ---

class PipelineRequest(BaseModel):
    categories: list[str] = [
        "top", "technology", "business", "science", "energy", "world", "health"
    ]
    force: bool = False


class CategoryResult(BaseModel):
    new_articles: int = 0
    skipped: int = 0
    errors: int = 0


class PipelineResponse(BaseModel):
    results: dict[str, CategoryResult]
    duration_ms: int


# --- Comparison (stub) ---

class CompareRequest(BaseModel):
    topic: str
    sources: list[str] = ["reuters", "bbc", "associated press"]


class CompareResponse(BaseModel):
    topic: str
    comparison: str
    sources_checked: list[str]
    claims: list[dict]
    duration_ms: int


# --- Health ---

class HealthResponse(BaseModel):
    status: str
    version: str
    db_connected: bool
    last_pipeline_run: str | None


# --- Internal: RSS article before summarization ---

class RSSArticle(BaseModel):
    title: str
    source_url: str
    source_name: str
    published_date: datetime | None = None
    image_url: str | None = None
    category: str
    raw_content: str = ""  # RSS description/content, used for summarization input
