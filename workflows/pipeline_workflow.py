from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import StateGraph, END

from app.models import RSSArticle, CategoryResult

logger = logging.getLogger("sift-api.pipeline")

ALL_CATEGORIES = ["top", "technology", "business", "science", "energy", "world", "health", "politics", "sports", "entertainment"]

# Skip-guard knobs for story threading (Phase 5).
# Threading is expensive (~39% of Anthropic spend) and re-clusters the full
# 48h window of articles every run. If only a handful of new articles
# arrived in a category since last run, the cluster result barely changes.
# Skip unless: (a) enough new articles arrived, OR (b) too much time passed.
MIN_NEW_ARTICLES_FOR_THREADING = 3
MAX_THREADING_INTERVAL_SECONDS = 30 * 60  # 30 min


class PipelineState(TypedDict):
    force: bool
    articles: list[RSSArticle]
    new_articles: list[RSSArticle]
    summaries: dict[str, dict]       # source_url -> {"summary": str, "category": str}
    contexts: dict[str, str]         # source_url -> "This matters because..."
    importance_scores: dict[str, int]  # source_url -> 1-5
    embeddings: dict[str, list[float]]  # source_url -> vector
    results: dict[str, CategoryResult]
    total_skipped: int
    errors: list[str]


# --- Node functions ---

async def fetch_rss_node(state: PipelineState) -> dict:
    """Fetch all RSS feeds."""
    from services.rss import fetch_feeds

    try:
        articles = await fetch_feeds()
        logger.info("fetch_rss: got %d articles", len(articles))
        return {"articles": articles}
    except Exception as e:
        logger.error("fetch_rss failed: %s", e)
        return {"articles": [], "errors": state.get("errors", []) + [f"fetch_rss: {e}"]}


async def deduplicate_node(state: PipelineState) -> dict:
    """Filter out articles already in the database."""
    from services.deduplicator import deduplicate

    articles = state.get("articles", [])
    if not articles:
        return {"new_articles": []}

    if state.get("force"):
        logger.info("deduplicate: force=True, keeping all %d articles", len(articles))
        return {"new_articles": articles}

    try:
        new = await deduplicate(articles)
        logger.info("deduplicate: %d new, %d skipped", len(new), len(articles) - len(new))
        return {"new_articles": new}
    except Exception as e:
        logger.error("deduplicate failed: %s", e)
        # On error, treat all as new (will fail at store if truly duplicates)
        return {
            "new_articles": articles,
            "errors": state.get("errors", []) + [f"deduplicate: {e}"],
        }


async def summarize_node(state: PipelineState) -> dict:
    """Batch-summarize and classify new articles using Claude Haiku."""
    from services.summarizer import summarize_articles

    new_articles = state.get("new_articles", [])
    if not new_articles:
        logger.info("summarize: no new articles to summarize")
        return {"summaries": {}, "new_articles": []}

    try:
        summaries = await summarize_articles(new_articles)
        logger.info("summarize: generated %d summaries with categories", len(summaries))

        # Apply AI-assigned categories back to each article
        for article in new_articles:
            result = summaries.get(article.source_url)
            if result:
                article.category = result["category"]
            else:
                article.category = "top"  # fallback

        return {"summaries": summaries, "new_articles": new_articles}
    except Exception as e:
        logger.error("summarize failed: %s", e)
        # Fall back to raw RSS content as summaries, default to "top" category
        fallback: dict[str, dict] = {}
        for article in new_articles:
            article.category = "top"
            if article.raw_content:
                words = article.raw_content.split()
                fallback[article.source_url] = {
                    "summary": " ".join(words[:50]),
                    "category": "top",
                }
        return {
            "summaries": fallback,
            "new_articles": new_articles,
            "errors": state.get("errors", []) + [f"summarize: {e}"],
        }


async def context_node(state: PipelineState) -> dict:
    """Generate 'why it matters' one-liners and importance scores using Claude Haiku."""
    from services.context_generator import generate_context

    new_articles = state.get("new_articles", [])
    summaries = state.get("summaries", {})
    if not new_articles:
        logger.info("context: no new articles to generate context for")
        return {"contexts": {}, "importance_scores": {}}

    # Build input: title + summary for each article
    articles_for_context = []
    for article in new_articles:
        result = summaries.get(article.source_url)
        summary = result["summary"] if result else ""
        if summary:
            articles_for_context.append({
                "source_url": article.source_url,
                "title": article.title,
                "summary": summary,
            })

    try:
        raw = await generate_context(articles_for_context)
        # Unpack: raw is {source_url: {"context": str, "score": int}}
        contexts: dict[str, str] = {}
        importance_scores: dict[str, int] = {}
        for url, data in raw.items():
            contexts[url] = data["context"]
            importance_scores[url] = data["score"]
        logger.info("context: generated %d context lines + scores", len(contexts))
        return {"contexts": contexts, "importance_scores": importance_scores}
    except Exception as e:
        logger.error("context failed: %s", e)
        return {
            "contexts": {},
            "importance_scores": {},
            "errors": state.get("errors", []) + [f"context: {e}"],
        }


async def embed_node(state: PipelineState) -> dict:
    """Generate Voyage AI embeddings for new articles."""
    from services.embedder import embed_texts

    new_articles = state.get("new_articles", [])
    if not new_articles:
        logger.info("embed: no new articles to embed")
        return {"embeddings": {}}

    summaries = state.get("summaries", {})

    # Build embedding input: title + summary (or raw content)
    texts = []
    for article in new_articles:
        result = summaries.get(article.source_url)
        summary = result["summary"] if result else article.raw_content
        texts.append(f"{article.title}. {summary}")

    try:
        vectors = await embed_texts(texts)
        embeddings = {
            article.source_url: vector
            for article, vector in zip(new_articles, vectors)
        }
        logger.info("embed: generated %d embeddings", len(embeddings))
        return {"embeddings": embeddings}
    except Exception as e:
        logger.error("embed failed: %s", e)
        return {
            "embeddings": {},
            "errors": state.get("errors", []) + [f"embed: {e}"],
        }


async def store_node(state: PipelineState) -> dict:
    """Upsert articles into Postgres and update pipeline_state."""
    from app.db import get_pool
    from services.rss import stable_hash

    new_articles = state.get("new_articles", [])
    summaries = state.get("summaries", {})
    contexts = state.get("contexts", {})
    importance_scores = state.get("importance_scores", {})
    embeddings = state.get("embeddings", {})
    all_articles = state.get("articles", [])

    pool = await get_pool()

    # Count new articles by category (categories are assigned during summarization)
    new_by_cat: dict[str, list[RSSArticle]] = {}
    for a in new_articles:
        cat = a.category or "top"
        new_by_cat.setdefault(cat, []).append(a)

    # Skipped = total fetched - total new (categories aren't available for
    # skipped articles since they never went through summarization, so we
    # can only report the total skipped count, not per-category)
    total_skipped = len(all_articles) - len(new_articles)

    results: dict[str, CategoryResult] = {}
    for cat in ALL_CATEGORIES:
        new_count = len(new_by_cat.get(cat, []))
        results[cat] = CategoryResult(
            new_articles=new_count,
            skipped=0,
            errors=0,
        )

    # Upsert each new article
    stored = 0
    for article in new_articles:
        article_id = stable_hash(article.source_url + article.title)
        result = summaries.get(article.source_url)
        summary = result["summary"] if result else ""
        category = article.category or "top"
        why_it_matters = contexts.get(article.source_url)
        importance_score = importance_scores.get(article.source_url)
        embedding = embeddings.get(article.source_url)
        read_time = max(1, len(summary.split()) // 200 + 1) if summary else 1

        # Format embedding as pgvector string
        embedding_str = None
        if embedding:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

        try:
            await pool.execute(
                """
                INSERT INTO articles (id, title, summary, source_url, source_name,
                    image_url, category, published_date, embedding, read_time,
                    why_it_matters, importance_score, content_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector, $10, $11, $12, $13)
                ON CONFLICT (source_url) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    category = EXCLUDED.category,
                    embedding = EXCLUDED.embedding,
                    why_it_matters = EXCLUDED.why_it_matters,
                    importance_score = EXCLUDED.importance_score,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = NOW()
                """,
                article_id,
                article.title,
                summary,
                article.source_url,
                article.source_name,
                article.image_url,
                category,
                article.published_date,
                embedding_str,
                read_time,
                why_it_matters,
                importance_score,
                article.content_hash,
            )
            stored += 1
        except Exception as e:
            logger.error("Failed to store article %s: %s", article.source_url, e)
            if category in results:
                results[category].errors += 1

    # Update pipeline_state for ALL categories
    for cat in ALL_CATEGORIES:
        try:
            count = await pool.fetchval(
                "SELECT COUNT(*) FROM articles WHERE category = $1 AND from_search = false", cat,
            )
            await pool.execute(
                """
                INSERT INTO pipeline_state (category, last_refreshed_at, article_count, error)
                VALUES ($1, NOW(), $2, NULL)
                ON CONFLICT (category) DO UPDATE SET
                    last_refreshed_at = NOW(),
                    article_count = $2,
                    error = NULL
                """,
                cat,
                count,
            )
        except Exception as e:
            logger.error("Failed to update pipeline_state for %s: %s", cat, e)

    logger.info("store: inserted %d articles", stored)

    # Run story threading for categories that received new articles.
    # Skip categories with <MIN_NEW_ARTICLES_FOR_THREADING new articles UNLESS
    # more than MAX_THREADING_INTERVAL_SECONDS has elapsed since the last
    # threading run for that category (uses stories.updated_at as the proxy).
    from workflows.story_workflow import run_story_threading

    categories_with_new = [cat for cat in new_by_cat if new_by_cat[cat]]
    now_utc = datetime.now(timezone.utc)

    for cat in categories_with_new:
        new_count = len(new_by_cat[cat])
        should_skip = False
        age_seconds: float | None = None

        if new_count < MIN_NEW_ARTICLES_FOR_THREADING:
            try:
                last_threaded = await pool.fetchval(
                    "SELECT MAX(updated_at) FROM stories WHERE category = $1", cat,
                )
            except Exception as e:
                logger.error("Failed to read last threading time for %s: %s", cat, e)
                last_threaded = None

            if last_threaded is not None:
                age_seconds = (now_utc - last_threaded).total_seconds()
                if age_seconds < MAX_THREADING_INTERVAL_SECONDS:
                    should_skip = True

        if should_skip:
            logger.info(json.dumps({
                "event": "threading_skipped",
                "category": cat,
                "new_articles": new_count,
                "seconds_since_last_threading": int(age_seconds) if age_seconds is not None else None,
                "reason": "below_threshold_within_window",
            }))
            continue

        try:
            await run_story_threading(cat)
            logger.info("story threading completed for %s", cat)
        except Exception as e:
            logger.error("story threading failed for %s: %s", cat, e)

    return {"results": results, "total_skipped": total_skipped}


# --- Build the graph ---

def build_pipeline_graph():
    """Build and compile the LangGraph pipeline workflow."""
    graph = StateGraph(PipelineState)

    graph.add_node("fetch_rss", fetch_rss_node)
    graph.add_node("deduplicate", deduplicate_node)
    graph.add_node("summarize", summarize_node)
    graph.add_node("context", context_node)
    graph.add_node("embed", embed_node)
    graph.add_node("store", store_node)

    graph.set_entry_point("fetch_rss")
    graph.add_edge("fetch_rss", "deduplicate")
    graph.add_edge("deduplicate", "summarize")
    graph.add_edge("summarize", "context")
    graph.add_edge("context", "embed")
    graph.add_edge("embed", "store")
    graph.add_edge("store", END)

    return graph.compile()
