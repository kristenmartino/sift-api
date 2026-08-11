"""
One-shot backfill of articles.genre (migrations/025) for recent articles.

Run from sift-api root:
    python scripts/backfill_genre.py            # last 7 days, live writes
    python scripts/backfill_genre.py --days 14
    python scripts/backfill_genre.py --dry-run  # classify + report only

New articles get genre from the extended context prompt; this covers rows
that predate it. Same shape as scripts/backfill_tone.py: a genre-only
mini-prompt (title + summary snippet), the shared alignment plumbing, and
_clamp_genre for validation. Deliberately not a generate_context re-run —
that would churn every gated why_it_matters line at ~5x the cost.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic  # noqa: E402
import asyncpg  # noqa: E402
from app.config import settings  # noqa: E402
from services.context_generator import MODEL, _clamp_genre, _extract_json_array  # noqa: E402
from services.index_alignment import AlignmentError, aligned_entries  # noqa: E402

BATCH_SIZE = 20
SNIPPET = 200


def _build_prompt(batch: list[dict]) -> str:
    articles_text = ""
    for i, a in enumerate(batch, 1):
        articles_text += f"\n{i}. \"{a['title']}\"\n   Summary: {(a['summary'] or '')[:SNIPPET]}\n"
    return f"""For each article below, provide a genre tag (key "g"). Exactly one of:
   "news" = a report of something that happened, however small — arrests, \
rulings, votes, disasters, earnings, announcements, live updates
   "feature" = magazine-style writing rather than reporting: narrative \
longform, profiles, retrospectives, human-interest storytelling about \
people rather than events
   "soft" = curiosity, lifestyle, celebrity gossip, viral oddities, \
service journalism ("what to know about", listicles, rankings)
   A reported event is "news" no matter how dramatic, tabloid, or minor it \
is — this tag is about the KIND of writing, not its importance or its \
subject. When unsure, choose "news".

Articles:
{articles_text}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, g=genre.
[{{"i":1,"g":"news"}}, ...]

Return ONLY the JSON array, no other text."""


async def classify(client: anthropic.AsyncAnthropic, batch: list[dict]) -> dict[str, str]:
    resp = await client.messages.create(
        model=MODEL, max_tokens=400,
        messages=[{"role": "user", "content": _build_prompt(batch)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    parsed = _extract_json_array(text)
    if parsed is None:
        raise AlignmentError("response was not a parseable JSON array")
    return {
        batch[i - 1]["source_url"]: _clamp_genre(e.get("g", e.get("genre")))
        for i, e in aligned_entries(parsed, len(batch)).items()
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    pool = await asyncpg.create_pool(
        db_url, min_size=1, max_size=5,
        ssl="require" if "neon.tech" in db_url else False,
    )
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    rows = await pool.fetch(
        """
        SELECT source_url, title, summary FROM articles
        WHERE genre IS NULL AND from_search = false
          AND summary IS NOT NULL AND summary != ''
          AND LOWER(summary) NOT LIKE 'unable to provide%'
          AND (published_date > NOW() - make_interval(days => $1)
               OR (published_date IS NULL AND created_at > NOW() - make_interval(days => $1)))
        ORDER BY published_date DESC NULLS LAST
        """,
        args.days,
    )
    if not rows:
        print("No articles need genre backfilling.")
        await pool.close()
        return

    print(f"Backfilling genre for {len(rows)} articles ({args.days}-day window)...")
    articles = [dict(r) for r in rows]
    counts = {"news": 0, "feature": 0, "soft": 0}
    failed = 0
    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            results = await classify(client, batch)
        except Exception as e:
            failed += 1
            print(f"  batch {i // BATCH_SIZE} failed ({e}); rows stay NULL (= news in ranking)")
            continue
        for source_url, genre in results.items():
            counts[genre] += 1
            if not args.dry_run:
                await pool.execute(
                    "UPDATE articles SET genre = $1 WHERE source_url = $2", genre, source_url
                )
        done = min(i + BATCH_SIZE, len(articles))
        if done % 200 < BATCH_SIZE or done == len(articles):
            print(f"  {done}/{len(articles)}  {counts}")

    verb = "Classified (dry run)" if args.dry_run else "Updated"
    print(f"\nDone. {verb} {sum(counts.values())}: {counts}. Failed batches: {failed}.")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
