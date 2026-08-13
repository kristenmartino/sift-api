"""
One-shot re-score of importance_score for recent grim articles.

Run from sift-api root:
    python scripts/rescore_importance.py            # last 7 days, live writes
    python scripts/rescore_importance.py --dry-run  # classify + report, no writes
    python scripts/rescore_importance.py --days 3

Why grim rows only: the pre-2026-08-11 rubric let "wide impact" pattern-match
onto emotional weight, so dramatic single-victim crime scored 4 — and at 4+
it is exempt from the D48 grim dampener, which is exactly the population that
was stacking the top of the feed. Non-grim scores showed no such inflation
and are left alone; new articles get the tightened rubric from the pipeline.

Uses IMPORTANCE_RUBRIC verbatim from services.context_generator so this
script and the live prompt cannot drift. Writes importance_score only —
why_it_matters and tone are untouched.
"""
import argparse
import asyncio
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import asyncpg
from app.config import settings
from services.context_generator import IMPORTANCE_RUBRIC, OPERATION, _extract_json_array
from services.model_registry import resolve
from services.index_alignment import AlignmentError, aligned_entries

BATCH_SIZE = 20
SUMMARY_SNIPPET_CHARS = 200


def _build_prompt(batch: list[dict]) -> str:
    articles_text = ""
    for i, article in enumerate(batch, 1):
        snippet = (article["summary"] or "")[:SUMMARY_SNIPPET_CHARS]
        articles_text += f"\n{i}. \"{article['title']}\"\n   Summary: {snippet}\n"

    return f"""For each article below, provide an importance score from 1-5 (key "s").
{IMPORTANCE_RUBRIC}

Articles:
{articles_text}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, s=score.
[{{"i":1,"s":2}}, ...]

Return ONLY the JSON array, no other text."""


def _clamp_score(raw: object) -> int:
    if isinstance(raw, int) and 1 <= raw <= 5:
        return raw
    return 3


async def rescore_batch(client: anthropic.AsyncAnthropic, batch: list[dict]) -> dict[str, int]:
    response = await client.messages.create(
        model=resolve(OPERATION).model,
        max_tokens=400,
        messages=[{"role": "user", "content": _build_prompt(batch)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = _extract_json_array(text)
    if parsed is None:
        raise AlignmentError("response was not a parseable JSON array")
    results: dict[str, int] = {}
    for idx, entry in aligned_entries(parsed, len(batch)).items():
        results[batch[idx - 1]["source_url"]] = _clamp_score(entry.get("s", entry.get("score")))
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    rows = await pool.fetch(
        """
        SELECT source_url, title, summary, importance_score
        FROM articles
        WHERE tone = 'grim'
          AND importance_score IS NOT NULL
          AND from_search = false
          AND summary IS NOT NULL AND summary != ''
          AND (published_date > NOW() - make_interval(days => $1)
               OR (published_date IS NULL AND created_at > NOW() - make_interval(days => $1)))
        ORDER BY published_date DESC NULLS LAST
        """,
        args.days,
    )
    if not rows:
        print("No grim articles to re-score.")
        await pool.close()
        return

    print(f"Re-scoring {len(rows)} grim articles ({args.days}-day window)...")
    articles = [dict(r) for r in rows]
    old_by_url = {a["source_url"]: a["importance_score"] for a in articles}
    moves = collections.Counter()
    updated = 0
    failed_batches = 0

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            results = await rescore_batch(client, batch)
        except Exception as e:
            failed_batches += 1
            print(f"  batch {i // BATCH_SIZE} failed ({e}); scores unchanged")
            continue

        for source_url, new_score in results.items():
            old = old_by_url[source_url]
            moves[(old, new_score)] += 1
            if not args.dry_run and new_score != old:
                await pool.execute(
                    "UPDATE articles SET importance_score = $1 WHERE source_url = $2",
                    new_score, source_url,
                )
                updated += 1

        done = min(i + BATCH_SIZE, len(articles))
        if done % 200 < BATCH_SIZE or done == len(articles):
            print(f"  {done}/{len(articles)}")

    print("\nScore moves (old -> new: count):")
    for (old, new), n in sorted(moves.items()):
        marker = "  =" if old == new else (" 🔻" if new < old else " 🔺")
        print(f"  {old} -> {new}: {n}{marker}")
    verb = "Would update" if args.dry_run else "Updated"
    changed = sum(n for (o, nw), n in moves.items() if o != nw)
    print(f"\n{verb} {changed} of {sum(moves.values())} scores. Failed batches: {failed_batches}.")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
