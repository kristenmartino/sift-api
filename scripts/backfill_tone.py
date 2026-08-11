"""
One-shot backfill of articles.tone (migrations/020) for recent feed articles.

Run from sift-api root:
    python scripts/backfill_tone.py            # last 7 days, live writes
    python scripts/backfill_tone.py --days 30  # wider window
    python scripts/backfill_tone.py --dry-run  # classify + print, no writes

New articles get tone from the extended context_generator prompt; this covers
the rows that predate it. 7 days is the useful horizon: the feed's recency
decay (e^-age_days) makes older rows unrankable, and NULL tone is already
treated as neutral (no penalty) everywhere.

Deliberately NOT a re-run of generate_context: that would regenerate and
churn every gated why_it_matters line and cost ~5x more. This uses a
tone-only mini-prompt over title + a summary snippet, with the same
alignment plumbing (aligned_entries) and the same _clamp_tone validation.
Cost: ~60 input tokens/article of Haiku — about $1 for a 7-day window.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import asyncpg
from app.config import settings
from services.context_generator import MODEL, _clamp_tone, _extract_json_array
from services.index_alignment import AlignmentError, aligned_entries

BATCH_SIZE = 20
SUMMARY_SNIPPET_CHARS = 200


def _build_prompt(batch: list[dict]) -> str:
    articles_text = ""
    for i, article in enumerate(batch, 1):
        snippet = (article["summary"] or "")[:SUMMARY_SNIPPET_CHARS]
        articles_text += f"\n{i}. \"{article['title']}\"\n   Summary: {snippet}\n"

    return f"""For each article below, provide a tone tag (key "t"). Exactly one of:
   "grim" = the event itself is death, killing, violent crime, serious \
injury, a fatal disaster or accident, abuse, or war casualties
   "light" = feel-good, humor, entertainment, culture, scientific wonder, \
sports achievement, positive milestone
   "neutral" = everything else — including serious-but-not-deadly news \
(economic trouble, political conflict, lawsuits, layoffs, fraud, policy fights)
   Judge the event, not the writing style: a dry report of a murder is \
"grim"; an alarmed report about interest rates is "neutral". When unsure \
between "grim" and "neutral", choose "neutral".

Articles:
{articles_text}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, t=tone.
[{{"i":1,"t":"neutral"}}, ...]

Return ONLY the JSON array, no other text."""


async def classify_batch(client: anthropic.AsyncAnthropic, batch: list[dict]) -> dict[str, str]:
    response = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": _build_prompt(batch)}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = _extract_json_array(text)
    if parsed is None:
        raise AlignmentError("response was not a parseable JSON array")
    results: dict[str, str] = {}
    for idx, entry in aligned_entries(parsed, len(batch)).items():
        results[batch[idx - 1]["source_url"]] = _clamp_tone(entry.get("t", entry.get("tone")))
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Window of recent articles to backfill (default 7).")
    parser.add_argument("--dry-run", action="store_true", help="Classify and print the distribution; write nothing.")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, ssl=ssl_mode)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    rows = await pool.fetch(
        """
        SELECT source_url, title, summary
        FROM articles
        WHERE tone IS NULL
          AND from_search = false
          AND summary IS NOT NULL AND summary != ''
          AND LOWER(summary) NOT LIKE 'unable to provide%'
          AND (published_date > NOW() - make_interval(days => $1)
               OR (published_date IS NULL AND created_at > NOW() - make_interval(days => $1)))
        ORDER BY published_date DESC NULLS LAST
        """,
        args.days,
    )
    if not rows:
        print("No articles need tone backfilling.")
        await pool.close()
        return

    print(f"Backfilling tone for {len(rows)} articles ({args.days}-day window)...")
    articles = [dict(r) for r in rows]
    counts = {"grim": 0, "neutral": 0, "light": 0}
    updated = 0
    failed_batches = 0

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        try:
            results = await classify_batch(client, batch)
        except Exception as e:
            failed_batches += 1
            print(f"  batch {i // BATCH_SIZE} failed ({e}); rows stay NULL (= neutral in ranking)")
            continue

        for source_url, tone in results.items():
            counts[tone] += 1
            if not args.dry_run:
                await pool.execute(
                    "UPDATE articles SET tone = $1 WHERE source_url = $2",
                    tone, source_url,
                )
                updated += 1

        done = min(i + BATCH_SIZE, len(articles))
        if done % 200 < BATCH_SIZE or done == len(articles):
            print(f"  {done}/{len(articles)}  {counts}")

    verb = "Classified (dry run)" if args.dry_run else "Updated"
    print(f"\nDone. {verb} {sum(counts.values())} articles: {counts}. Failed batches: {failed_batches}.")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
