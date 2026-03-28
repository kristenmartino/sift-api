from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, END

from app.models import RSSArticle, CategoryResult

logger = logging.getLogger("sift-api.pipeline")


class PipelineState(TypedDict):
    categories: list[str]
    force: bool
    articles: list[RSSArticle]
    new_articles: list[RSSArticle]
    summaries: dict[str, str]       # source_url -> summary
    embeddings: dict[str, list[float]]  # source_url -> vector
    results: dict[str, CategoryResult]
    errors: list[str]


# --- Node functions ---

async def fetch_rss_node(state: PipelineState) -> dict:
    """Fetch RSS feeds for all requested categories."""
    from services.rss import fetch_feeds

    try:
        articles = await fetch_feeds(state["categories"])
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
    """Batch-summarize new articles using Claude Haiku."""
    from services.summarizer import summarize_articles

    new_articles = state.get("new_articles", [])
    if not new_articles:
        logger.info("summarize: no new articles to summarize")
        return {"summaries": {}}

    try:
        summaries = await summarize_articles(new_articles)
        logger.info("summarize: generated %d summaries", len(summaries))
        return {"summaries": summaries}
    except Exception as e:
        logger.error("summarize failed: %s", e)
        # Fall back to raw RSS content as summaries
        fallback = {}
        for article in new_articles:
            if article.raw_content:
                words = article.raw_content.split()
                fallback[article.source_url] = " ".join(words[:50])
        return {
            "summaries": fallback,
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
        summary = summaries.get(article.source_url, article.raw_content)
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

    # Count by category for results
    all_by_cat: dict[str, int] = {}
    for a in all_articles:
        all_by_cat[a.category] = all_by_cat.get(a.category, 0) + 1

    new_by_cat: dict[str, list[RSSArticle]] = {}
    for a in new_articles:
        new_by_cat.setdefault(a.category, []).append(a)

    results: dict[str, CategoryResult] = {}
    for cat in state["categories"]:
        new_count = len(new_by_cat.get(cat, []))
        total_fetched = all_by_cat.get(cat, 0)
        results[cat] = CategoryResult(
            new_articles=new_count,
            skipped=total_fetched - new_count,
            errors=0,
        )

    # Upsert each new article
    stored = 0
    for article in new_articles:
        article_id = stable_hash(article.source_url + article.title)
        summary = summaries.get(article.source_url, "")
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
                    embedding = EXCLUDED.embedding,
                    updated_at = NOW()
                """,
                article_id,
                article.title,
                summary,
                article.source_url,
                article.source_name,
                article.image_url,
                article.category,
                article.published_date,
                embedding_str,
                read_time,
            )
            stored += 1
        except Exception as e:
            logger.error("Failed to store article %s: %s", article.source_url, e)
            if article.category in results:
                results[article.category].errors += 1

    # Update pipeline_state for each category
    for cat in state["categories"]:
        try:
            count = await pool.fetchval(
                "SELECT COUNT(*) FROM articles WHERE category = $1", cat,
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
