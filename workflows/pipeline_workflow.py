from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, END

from app.models import RSSArticle, CategoryResult

logger = logging.getLogger("sift-api.pipeline")

ALL_CATEGORIES = ["top", "technology", "business", "science", "energy", "world", "health", "politics", "sports", "entertainment"]


class PipelineState(TypedDict):
    force: bool
    articles: list[RSSArticle]
    new_articles: list[RSSArticle]
    summaries: dict[str, dict]       # source_url -> {"summary": str, "category": str}
    embeddings: dict[str, list[float]]  # source_url -> vector
    results: dict[str, CategoryResult]
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

    # Attribute total skipped count to "top" as a summary figure
    if total_skipped > 0:
        results["top"].skipped = total_skipped

    # Upsert each new article
    stored = 0
    for article in new_articles:
        article_id = stable_hash(article.source_url + article.title)
        result = summaries.get(article.source_url)
        summary = result["summary"] if result else ""
        category = article.category or "top"
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
                    image_url, category, published_date, embedding, read_time)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector, $10)
                ON CONFLICT (source_url) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    category = EXCLUDED.category,
                    embedding = EXCLUDED.embedding,
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
    return {"results": results}


# --- Build the graph ---

def build_pipeline_graph():
    """Build and compile the LangGraph pipeline workflow."""
    graph = StateGraph(PipelineState)

    graph.add_node("fetch_rss", fetch_rss_node)
    graph.add_node("deduplicate", deduplicate_node)
    graph.add_node("summarize", summarize_node)
    graph.add_node("embed", embed_node)
    graph.add_node("store", store_node)

    graph.set_entry_point("fetch_rss")
    graph.add_edge("fetch_rss", "deduplicate")
    graph.add_edge("deduplicate", "summarize")
    graph.add_edge("summarize", "embed")
    graph.add_edge("embed", "store")
    graph.add_edge("store", END)

    return graph.compile()
