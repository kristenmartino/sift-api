"""Repair articles confirmed to be carrying another article's summary (#117).

The second half of `scripts/find_misaligned_summaries.py`. That script FINDS
candidates and never writes; this one fixes the ones you have confirmed by
hand. Confirmation is deliberately a human step — the detector's own top hits
include legitimate oblique headlines, and re-summarizing a correct row costs a
model call and gains nothing.

Two things make this more than an UPDATE of one column.

**The body text is gone.** `articles` stores title and summary, never the
article body (deliberately — see the DMCA/fair-use posture in STATUS.md). So a
correct summary cannot be regenerated from the row. The only recovery route is
the outlet's live RSS feed, which carries roughly a day of items: recent
articles can be re-summarized, older ones cannot. When recovery fails, this
script BLANKS the summary rather than leaving false text under a real
headline. An empty card is a degraded card; a card claiming a murdered
couple's story is about a stalled DOJ nomination is a wrong one.

**Everything downstream of the summary is poisoned too.** These are all
computed from title + summary, so a swap corrupts them as well, and none of
them announce it:

    embedding        title + summary -> the vector, so search and story
                     threading both see the wrong article
    why_it_matters   generated from title + summary
    importance_score same call
    context_primer   same inputs
    entities         extracted from title + summary
    entity_links     linked over title + summary
    reading_levels   rewrites OF the summary
    story_id         clustered on title/summary/entities

Repairing the summary alone would leave a row that looks fixed and still
behaves wrong. So each repaired row has its derived fields cleared; the
backfill scripts (`backfill_context.py`, `backfill_primers.py`,
`backfill_entity_links.py`, `backfill_embeddings.py`) and the next threading
run regenerate them from the corrected text.

DRY RUN by default — prints the plan and writes nothing. Pass --apply.

Examples:
  # confirm rows by putting "real" in the verdict column of the triage CSV
  ./.venv/bin/python3 scripts/repair_misaligned_summaries.py --csv data/_cache/misaligned_candidates.csv
  ./.venv/bin/python3 scripts/repair_misaligned_summaries.py --csv data/_cache/misaligned_candidates.csv --apply
  ./.venv/bin/python3 scripts/repair_misaligned_summaries.py --url https://example.com/a --apply
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import RSSArticle  # noqa: E402

# Verdicts in the CSV's `verdict` column that mean "yes, this row is broken".
CONFIRMED = {"real", "confirmed", "yes", "y", "swap", "misaligned"}


async def _connect() -> asyncpg.Pool:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    return await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)


def urls_from_csv(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and "verdict" not in rows[0]:
        raise SystemExit(f"{path} has no `verdict` column — is it a triage CSV?")
    return [r["source_url"] for r in rows if r.get("verdict", "").strip().lower() in CONFIRMED]


async def recover_raw_content(urls: set[str]) -> dict[str, RSSArticle]:
    """Re-fetch every feed and return the still-published targets, by URL.

    One pass over all 58 feeds — cheaper and far kinder than fetching each
    article's page, and it stays inside the same RSS-only boundary the
    pipeline already operates in. Anything that has aged out of its feed is
    simply absent from the result.
    """
    from services.rss import fetch_feeds

    articles = await fetch_feeds()
    return {a.source_url: a for a in articles if a.source_url in urls and a.raw_content}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", help="triage CSV with a filled-in `verdict` column")
    parser.add_argument("--url", action="append", default=[], help="repair one URL (repeatable)")
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = parser.parse_args()

    targets = list(args.url)
    if args.csv:
        targets += urls_from_csv(args.csv)
    targets = list(dict.fromkeys(targets))  # de-dup, keep order
    if not targets:
        raise SystemExit("nothing to repair — pass --url, or mark rows 'real' in the CSV's verdict column")

    pool = await _connect()
    try:
        rows = await pool.fetch(
            "SELECT source_url, title, source_name, summary, category FROM articles "
            "WHERE source_url = ANY($1::text[])",
            targets,
        )
        by_url = {r["source_url"]: r for r in rows}
        missing = [u for u in targets if u not in by_url]
        for u in missing:
            print(f"  NOT IN DB   {u}")
        print(f"\n{len(by_url)}/{len(targets)} target rows found; checking live feeds for their text...")

        recovered = await recover_raw_content(set(by_url))
        print(f"{len(recovered)} still carried by their outlet's RSS feed (the rest have aged out)\n")

        resummarized = 0
        blanked = 0
        for url, row in by_url.items():
            print(f"- {row['title'][:78]}")
            print(f"    was: {(row['summary'] or '')[:96]}")

            new_summary = ""
            new_category = row["category"]
            embedding_str = None

            article = recovered.get(url)
            if article:
                # Re-summarize ONE article at a time. summarize_articles now
                # rejects any response whose indices are not exactly {1..n}
                # (#117), and a batch of one makes that guarantee trivial —
                # this is the repair path, so correctness beats token economy.
                from services.summarizer import summarize_articles

                article.title = row["title"]
                result = (await summarize_articles([article])).get(url)
                if result and result["summary"]:
                    new_summary = result["summary"]
                    new_category = result["category"]
                    resummarized += 1
                    print(f"    now: {new_summary[:96]}")

                    from services.embedder import embed_texts

                    vectors = await embed_texts([f"{row['title']}. {new_summary}"])
                    if vectors and vectors[0]:
                        embedding_str = "[" + ",".join(str(x) for x in vectors[0]) + "]"

            if not new_summary:
                blanked += 1
                print("    now: <blank — not in any live feed, so no text to summarize from>")

            if not args.apply:
                continue

            read_time = max(1, len(new_summary.split()) // 200 + 1) if new_summary else 1
            await pool.execute(
                """
                UPDATE articles
                   SET summary = $1,
                       category = $2,
                       read_time = $3,
                       embedding = $4::vector,
                       -- Everything below was generated FROM the wrong
                       -- summary. Cleared so the backfill scripts and the
                       -- next threading run rebuild it from the new text.
                       why_it_matters = NULL,
                       importance_score = NULL,
                       context_primer = NULL,
                       reading_levels = NULL,
                       entities = '[]'::jsonb,
                       entity_links = '[]'::jsonb,
                       story_id = NULL,
                       updated_at = NOW()
                 WHERE source_url = $5
                """,
                new_summary, new_category, read_time, embedding_str, url,
            )
    finally:
        await pool.close()

    verb = "repaired" if args.apply else "would repair"
    print(f"\n{verb}: {resummarized} re-summarized, {blanked} blanked (no recoverable text)")
    if not args.apply:
        print("DRY RUN — nothing was written. Re-run with --apply.")
    else:
        print("Derived fields cleared. Regenerate with: backfill_context.py, "
              "backfill_primers.py, backfill_entity_links.py, backfill_embeddings.py")


if __name__ == "__main__":
    asyncio.run(main())
