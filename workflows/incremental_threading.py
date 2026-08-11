"""Incremental story threading: consume a queue, don't rescan a window.

The write half of docs/INCREMENTAL_THREADING.md. The candidate half lives in
`services/story_matcher` and the confirmation call in `services/story_confirmer`.

WHY STORY IDENTITY CHANGES HERE
-------------------------------
`workflows/story_workflow.py` derives `story_id` from a sha256 of its sorted
member ids. That makes identity a function of current membership, so the
moment a story gains an article it becomes a *different story* — a new row,
while the old one is orphaned. Measured 2026-08-05: **58,259 of 58,557 rows
(99.5%) have no members.** The blanket `UPDATE articles SET story_id = NULL`
at the top of each run is the other half of the same mechanism.

Here a story is an entity with identity. Its id is derived once, from the
seed members that created it, and never changes as it grows. Attaching an
article writes one `story_id` and leaves the row alone. Nothing is orphaned,
because nothing is replaced.

Deriving the seed id deterministically (rather than a random uuid) keeps the
run idempotent: re-processing the same seed pair resolves to the same story
instead of making a duplicate.

WHAT COSTS MONEY
----------------
One `story_confirmer.confirm` call per run, over candidates Postgres already
filtered — against the old path's ~5.4 clusterer calls plus ~23 synthesize
calls. Synthesis now runs only when a story's *outlet set* changes, because
that is what its framings are built from; a second article from an outlet the
story already has adds nothing to synthesize.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from services.cost_guard import check_budget

logger = logging.getLogger("sift-api.incremental_threading")

# A story must reflect cross-outlet coverage, not one outlet publishing four
# near-duplicates. Same gate as story_workflow.py:196 — the UI renders "how N
# outlets covered this", so a single-outlet story misrepresents itself.
MIN_UNIQUE_OUTLETS = 2

# Pre-call cost estimates for the budget check, measured from `ai_usage_daily`
# over 7 days to 2026-08-11 (docs/SOURCE_SCALING.md): the confirmer ran $1.45
# across 230 calls, the synthesizer $10.14 across 4,564. The synthesis figure is
# an upper bound per relevant candidate — most attaches skip synthesis entirely
# because the outlet set did not change — so the guard trips slightly early,
# which is the right direction for a ceiling.
CONFIRM_COST_PER_CALL_USD = 0.0063
SYNTHESIS_COST_PER_CALL_USD = 0.0022

# Mirrors `services.story_confirmer.BATCH_SIZE`; imported lazily elsewhere in
# this module, so it is restated rather than imported at module scope.
_CONFIRM_BATCH_SIZE = 40


def _confirm_batches(relevant: list) -> int:
    return -(-len(relevant) // _CONFIRM_BATCH_SIZE)


def seed_story_id(category: str, member_ids: list[str]) -> str:
    """Derive a story's permanent id from the members that created it.

    Deterministic so a repeated seed is idempotent; derived from the *seed*
    only so later joiners never change it. That second property is the whole
    fix — see the module docstring.
    """
    key = category + "|" + "|".join(sorted(member_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def _story_outlets(pool, story_id: str) -> set[str]:
    rows = await pool.fetch(
        "SELECT DISTINCT source_name FROM articles WHERE story_id = $1", story_id,
    )
    return {r["source_name"] for r in rows if r["source_name"]}


async def _attach(pool, article: dict, story_id: str, synthesize) -> str:
    """Attach one article to an existing story. Returns what was done.

    Re-synthesizes only when the article brings an outlet the story did not
    already have, because `framings` is a per-outlet structure. A story
    gaining its third piece from an outlet it already carries has nothing new
    to say, and re-synthesizing it is exactly the duplicate spend #129 removed.
    """
    outlets_before = await _story_outlets(pool, story_id)
    await pool.execute(
        "UPDATE articles SET story_id = $1 WHERE id = $2", story_id, article["id"],
    )

    new_outlet = (article.get("source_name") or "") not in outlets_before
    if not new_outlet:
        await pool.execute(
            """UPDATE stories SET article_count = (
                   SELECT count(*) FROM articles WHERE story_id = $1
               ), updated_at = NOW() WHERE id = $1""",
            story_id,
        )
        return "attached_same_outlet"

    members = [dict(r) for r in await pool.fetch(
        """SELECT id, title, summary, source_name, source_url, image_url, published_date
           FROM articles WHERE story_id = $1""", story_id,
    )]
    synthesis = await synthesize(members)
    await pool.execute(
        """UPDATE stories
              SET headline = $2, summary = $3, framings = $4::jsonb,
                  article_count = $5, synthesis_status = 'complete', updated_at = NOW()
            WHERE id = $1""",
        story_id, synthesis["headline"], synthesis["summary"],
        json.dumps(synthesis.get("framings", [])), len(members),
    )
    return "attached_new_outlet_resynthesized"


async def _create(pool, article: dict, member_ids: list[str], synthesize) -> str | None:
    """Create a story from a confirmed new cluster. Returns its id, or None.

    Only claims members that are still unattached AT WRITE TIME. `find_candidates`
    snapshots the whole queue before any decision is applied, so one article can
    be offered as a loose neighbour to several candidates in the same run. Without
    this filter the later create re-points it with
    `UPDATE articles SET story_id`, silently stripping the earlier story.

    Measured on the first live run, 2026-08-10: **7 of 54 new stories lost
    members that way**, three of them down to zero. One was created with five
    members and kept none. That is the orphan mechanism this design exists to
    remove, reappearing through a different door — so the guard is here, against
    the database, rather than only in the caller's bookkeeping.
    """
    members = [dict(r) for r in await pool.fetch(
        """SELECT id, title, summary, source_name, source_url, image_url, published_date
           FROM articles
           WHERE id = ANY($1::text[])
             AND (story_id IS NULL OR id = $2)""",
        [article["id"], *member_ids], article["id"],
    )]
    outlets = {m["source_name"] for m in members if m.get("source_name")}
    if len(outlets) < MIN_UNIQUE_OUTLETS:
        logger.info(json.dumps({
            "event": "cluster_dropped_single_outlet",
            "category": article.get("category"),
            "outlet": next(iter(outlets), "unknown"),
            "article_count": len(members),
        }))
        return None

    story_id = seed_story_id(article.get("category") or "", [m["id"] for m in members])

    # An identical seed already resolved to this story on an earlier run.
    # Idempotent by construction: attach and skip the paid call.
    if await pool.fetchval("SELECT 1 FROM stories WHERE id = $1", story_id):
        await pool.execute(
            "UPDATE articles SET story_id = $1 WHERE id = ANY($2::text[])",
            story_id, [m["id"] for m in members],
        )
        return story_id

    synthesis = await synthesize(members)

    image = next((m["image_url"] for m in members if m.get("image_url")), None)
    dates = [m["published_date"] for m in members if m.get("published_date")]
    earliest = min(dates) if dates else None
    if isinstance(earliest, str):
        try:
            earliest = datetime.fromisoformat(earliest)
        except ValueError:
            earliest = None

    await pool.execute(
        """
        INSERT INTO stories (id, headline, summary, category, framings, entities,
            article_count, representative_image_url, published_date, synthesis_status)
        VALUES ($1, $2, $3, $4, $5::jsonb, '[]'::jsonb, $6, $7, $8, 'complete')
        ON CONFLICT (id) DO NOTHING
        """,
        story_id, synthesis["headline"], synthesis["summary"],
        article.get("category"), json.dumps(synthesis.get("framings", [])),
        len(members), image, earliest,
    )
    await pool.execute(
        "UPDATE articles SET story_id = $1 WHERE id = ANY($2::text[])",
        story_id, [m["id"] for m in members],
    )
    return story_id


async def run_incremental_threading(
    pool, *, candidates=None, confirm=None, synthesize=None,
) -> dict:
    """One incremental threading pass over the whole queue, all categories.

    Every queued article is marked threaded at the end whatever happened,
    including ones that matched nothing. A parked singleton stays searchable
    as a kNN neighbour, so a later arrival can still pull it into a story —
    it just stops being re-queued, which is what keeps the work O(new).

    `candidates`, `confirm` and `synthesize` are injectable so the decision
    and write logic can be exercised without a database or an API key, the
    same seam `cluster_articles` and `summarize_articles` gained for replay
    testing after #117.
    """
    from services.story_confirmer import confirm as _confirm
    from services.story_matcher import (
        fetch_queue,
        find_candidates,
        mark_threaded,
        summarize,
    )
    from services.story_synthesizer import synthesize_story

    confirm = confirm or _confirm
    synthesize = synthesize or synthesize_story

    if candidates is None:
        queue = await fetch_queue(pool)
        if not queue:
            return {"queued": 0}
        candidates = await find_candidates(pool, queue)

    if not candidates:
        return {"queued": 0}

    stats = summarize(candidates)
    relevant = [c for c in candidates if c["existing_stories"] or c["loose_neighbours"]]

    # Daily AI cost ceiling, covering both paid calls this run makes: one
    # confirmer call per 40 candidates, then up to one synthesis per decision.
    #
    # THE GUARD LIVES HERE RATHER THAN INSIDE `synthesize_story` ON PURPOSE.
    # `synthesize_story` degrades by returning `_fallback()` — the first
    # article's headline, flagged `_failed`. `story_workflow.py:246` reads that
    # flag and stores `synthesis_status='failed'` so it can be retried, but
    # `_attach` and `_create` below write `'complete'` unconditionally, so on
    # the live incremental path a fallback would be stored as a finished story
    # and never revisited. Skipping the whole run instead leaves every article
    # queued — `mark_threaded` is not reached — so the next run redoes it
    # properly once the budget resets.
    budget = await check_budget(
        CONFIRM_COST_PER_CALL_USD * _confirm_batches(relevant)
        + SYNTHESIS_COST_PER_CALL_USD * len(relevant)
    )
    if not budget.allowed:
        logger.warning(json.dumps({
            "event": "incremental_threading_skipped",
            "reason": budget.reason,
            "queued": len(candidates),
            "relevant": len(relevant),
            "note": "articles left unmarked; next run re-threads them",
        }))
        return {"queued": len(candidates), "skipped": budget.reason}

    decisions = await confirm(relevant) if relevant else {}

    applied = {"attached": 0, "resynthesized": 0, "created": 0,
               "dropped_single_outlet": 0, "none": 0, "already_claimed": 0}

    # Articles this run has already put into a story. Candidates were all
    # generated against one snapshot, so the same loose neighbour is routinely
    # offered to several of them; the first claim wins and the rest must not
    # re-point it. `_create` enforces this against the database too — this set
    # exists so a doomed decision is skipped before it costs a synthesis call.
    claimed: set[str] = set()

    for c in relevant:
        article = c["article"]
        d = decisions.get(article["id"]) or {"action": "none"}
        try:
            if article["id"] in claimed:
                # An earlier candidate already pulled this article into a
                # story. Acting again would move it and strip that story.
                applied["already_claimed"] += 1
                continue

            if d["action"] == "attach":
                what = await _attach(pool, article, d["story_id"], synthesize)
                claimed.add(article["id"])
                applied["attached"] += 1
                if what == "attached_new_outlet_resynthesized":
                    applied["resynthesized"] += 1
            elif d["action"] == "new":
                members = [m for m in d["members"] if m not in claimed]
                if not members:
                    # Every proposed member is spoken for; what remains is a
                    # single article, which is not a story.
                    applied["already_claimed"] += 1
                    continue
                sid = await _create(pool, article, members, synthesize)
                if sid:
                    claimed.add(article["id"])
                    claimed.update(members)
                    applied["created"] += 1
                else:
                    applied["dropped_single_outlet"] += 1
            else:
                applied["none"] += 1
        except Exception as e:  # noqa: BLE001 — one bad article must not stall the queue
            logger.error("threading %s failed: %s", article["id"], e)
            applied["none"] += 1

    # Mark the whole queue, not just the relevant subset: the parked ones are
    # precisely what must not come back next run.
    #
    # Guarded because this runs after the writes. A failure here is recoverable
    # — the articles stay queued and are reconsidered next run, where an
    # already-attached one is simply re-confirmed — but letting it propagate
    # would take down the pipeline node over bookkeeping.
    try:
        await mark_threaded(pool, [c["article"]["id"] for c in candidates])
    except Exception as e:  # noqa: BLE001
        logger.error("mark_threaded failed; queue will be reconsidered next run: %s", e)
        applied["mark_failed"] = True

    report = {"event": "incremental_threading", **stats, **applied}
    logger.info(json.dumps(report))
    return report
