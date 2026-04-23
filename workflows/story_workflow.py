from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import TypedDict

from langgraph.graph import StateGraph, END

logger = logging.getLogger("sift-api.story_workflow")

RECENCY_WINDOW_HOURS = 48


# ─── State ────────────────────────────────────────────────

class StoryState(TypedDict):
    category: str
    articles: list[dict]            # from DB: id, source_url, title, summary, source_name, image_url, published_date
    entities: dict[str, dict]       # source_url -> {people, organizations, locations, event_description}
    clusters: list[dict]            # [{group_id, article_indices, event}]
    stories: list[dict]             # synthesized: [{story_id, headline, summary, framings, article_urls}]
    errors: list[str]


# ─── Node 1: Fetch recent articles from DB ────────────────

async def fetch_articles_node(state: StoryState) -> dict:
    """Pull recent articles (48h) for the given category from Postgres."""
    from app.db import get_pool

    category = state["category"]
    pool = await get_pool()

    try:
        rows = await pool.fetch(
            f"""
            SELECT id, source_url, source_name, title, summary, image_url,
                   published_date, story_id
            FROM articles
            WHERE category = $1
              AND from_search = false
              AND published_date > NOW() - INTERVAL '{RECENCY_WINDOW_HOURS} hours'
              AND embedding IS NOT NULL
            ORDER BY published_date DESC
            LIMIT 50
            """,
            category,
        )

        articles = [
            {
                "id": row["id"],
                "source_url": row["source_url"],
                "source_name": row["source_name"],
                "title": row["title"],
                "summary": row["summary"] or "",
                "image_url": row["image_url"],
                "published_date": row["published_date"].isoformat() if row["published_date"] else None,
                "existing_story_id": row["story_id"],
            }
            for row in rows
        ]

        logger.info("fetch_articles [%s]: got %d articles from last %dh", category, len(articles), RECENCY_WINDOW_HOURS)
        return {"articles": articles}
    except Exception as e:
        logger.error("fetch_articles [%s] failed: %s", category, e)
        return {"articles": [], "errors": state.get("errors", []) + [f"fetch_articles: {e}"]}


# ─── Node 2: Entity extraction ────────────────────────────

async def extract_entities_node(state: StoryState) -> dict:
    """Batch entity extraction via Claude Haiku."""
    from services.entity_extractor import extract_entities

    articles = state.get("articles", [])
    if not articles:
        return {"entities": {}}

    try:
        entities = await extract_entities(articles)
        logger.info("extract_entities [%s]: extracted for %d articles", state["category"], len(entities))
        return {"entities": entities}
    except Exception as e:
        logger.error("extract_entities [%s] failed: %s", state["category"], e)
        return {
            "entities": {},
            "errors": state.get("errors", []) + [f"extract_entities: {e}"],
        }


# ─── Node 3: LLM clustering ──────────────────────────────

async def cluster_node(state: StoryState) -> dict:
    """LLM-as-judge: group articles about the same event."""
    from services.story_clusterer import cluster_articles

    articles = state.get("articles", [])
    entities = state.get("entities", {})

    if len(articles) < 2:
        return {"clusters": []}

    # Enrich articles with entities for the clustering prompt
    enriched = []
    for article in articles:
        enriched.append({
            **article,
            "entities": entities.get(article["source_url"], {}),
        })

    try:
        clusters = await cluster_articles(enriched)
        logger.info("cluster [%s]: found %d story groups", state["category"], len(clusters))
        return {"clusters": clusters}
    except Exception as e:
        logger.error("cluster [%s] failed: %s", state["category"], e)
        return {
            "clusters": [],
            "errors": state.get("errors", []) + [f"cluster: {e}"],
        }


# ─── Node 4: Synthesis + store ─────────────────────────────

async def synthesize_and_store_node(state: StoryState) -> dict:
    """Synthesize each cluster and persist stories to DB."""
    from app.db import get_pool
    from services.story_synthesizer import synthesize_story

    articles = state.get("articles", [])
    entities = state.get("entities", {})
    clusters = state.get("clusters", [])
    category = state["category"]

    if not clusters:
        return {"stories": []}

    pool = await get_pool()
    stories: list[dict] = []

    # First, clear stale story_id assignments for this category
    # (articles may have been re-clustered differently)
    await pool.execute(
        """
        UPDATE articles SET story_id = NULL
        WHERE category = $1
          AND story_id IS NOT NULL
          AND published_date > NOW() - INTERVAL '%s hours'
        """ % RECENCY_WINDOW_HOURS,
        category,
    )

    for cluster in clusters:
        indices = cluster.get("article_indices", [])
        event = cluster.get("event", "")

        # Map 1-based indices to articles
        cluster_articles = []
        for idx in indices:
            if 1 <= idx <= len(articles):
                cluster_articles.append(articles[idx - 1])

        if len(cluster_articles) < 2:
            continue

        # Generate stable story ID from sorted article IDs
        sorted_ids = sorted(a["id"] for a in cluster_articles)
        story_id = hashlib.sha256("|".join(sorted_ids).encode()).hexdigest()[:16]

        # Synthesize
        try:
            synthesis = await synthesize_story(cluster_articles)
        except Exception as e:
            logger.error("synthesis failed for cluster '%s': %s", event, e)
            synthesis = {
                "headline": cluster_articles[0]["title"],
                "summary": cluster_articles[0]["summary"],
                "framings": [],
                "_failed": True,
            }

        synthesis_status = "failed" if synthesis.get("_failed") else "complete"

        # Collect entities for the story
        story_entities = []
        for a in cluster_articles:
            ent = entities.get(a["source_url"], {})
            if ent:
                story_entities.append(ent)

        # Find representative image and earliest date
        representative_image = None
        earliest_date = None
        for a in cluster_articles:
            if not representative_image and a.get("image_url"):
                representative_image = a["image_url"]
            pd = a.get("published_date")
            if pd and (earliest_date is None or pd < earliest_date):
                earliest_date = pd

        # published_date is stored as an ISO string in state (see fetch_articles_node);
        # asyncpg requires datetime. Coerce back before the INSERT.
        if isinstance(earliest_date, str):
            try:
                earliest_date = datetime.fromisoformat(earliest_date)
            except ValueError:
                earliest_date = None

        # Upsert story
        try:
            await pool.execute(
                """
                INSERT INTO stories (id, headline, summary, category, framings, entities,
                    article_count, representative_image_url, published_date, synthesis_status)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10)
                ON CONFLICT (id) DO UPDATE SET
                    headline = EXCLUDED.headline,
                    summary = EXCLUDED.summary,
                    framings = EXCLUDED.framings,
                    entities = EXCLUDED.entities,
                    article_count = EXCLUDED.article_count,
                    representative_image_url = EXCLUDED.representative_image_url,
                    synthesis_status = EXCLUDED.synthesis_status,
                    updated_at = NOW()
                """,
                story_id,
                synthesis["headline"],
                synthesis["summary"],
                category,
                json.dumps(synthesis.get("framings", [])),
                json.dumps(story_entities),
                len(cluster_articles),
                representative_image,
                earliest_date,
                synthesis_status,
            )

            # Update story_id and entities on member articles
            for a in cluster_articles:
                ent = entities.get(a["source_url"], {})
                await pool.execute(
                    "UPDATE articles SET story_id = $1, entities = $2::jsonb WHERE id = $3",
                    story_id, json.dumps(ent), a["id"],
                )

            stories.append({
                "story_id": story_id,
                "headline": synthesis["headline"],
                "article_count": len(cluster_articles),
                "status": synthesis_status,
            })

        except Exception as e:
            logger.error("Failed to store story %s: %s", story_id, e)

    logger.info("synthesize_and_store [%s]: created/updated %d stories", category, len(stories))
    return {"stories": stories}


# ─── Build the graph ──────────────────────────────────────

def build_story_graph():
    """Build and compile the 4-node story threading LangGraph workflow."""
    graph = StateGraph(StoryState)

    graph.add_node("fetch_articles", fetch_articles_node)
    graph.add_node("extract_entities", extract_entities_node)
    graph.add_node("cluster", cluster_node)
    graph.add_node("synthesize_and_store", synthesize_and_store_node)

    graph.set_entry_point("fetch_articles")
    graph.add_edge("fetch_articles", "extract_entities")
    graph.add_edge("extract_entities", "cluster")
    graph.add_edge("cluster", "synthesize_and_store")
    graph.add_edge("synthesize_and_store", END)

    return graph.compile()


# ─── Public API ───────────────────────────────────────────

_story_graph = None


def _get_graph():
    global _story_graph
    if _story_graph is None:
        _story_graph = build_story_graph()
    return _story_graph


async def run_story_threading(category: str) -> dict:
    """Run the story threading workflow for a single category."""
    graph = _get_graph()
    initial_state: StoryState = {
        "category": category,
        "articles": [],
        "entities": {},
        "clusters": [],
        "stories": [],
        "errors": [],
    }
    result = await graph.ainvoke(initial_state)
    return result
