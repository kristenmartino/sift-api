"""Triage candidates for the summarizer misalignment bug (services/summarizer.py).

Before the alignment fix, `_parse_summaries` mapped each summary back to an
article using the MODEL'S OWN returned index and checked only that the index
was in range. A repeated, skipped, or shifted index attached a summary to the
WRONG article and the pipeline reported success. Rows written before the fix
can still carry another article's summary.

This script FINDS candidates. It does not repair anything and never writes.

  Detector A (cheap, noisy): zero content-word overlap between title and
  summary. Over a 7-day window this flagged 208 / 12,436 rows (1.67%) — an
  OVERSTATEMENT of the bug. Most flags are legitimate oblique headlines
  ("Liars and Loons" correctly summarized as a Fauci opinion piece). Do not
  quote that rate as the bug rate.

  Detector B (the useful one): for each row flagged by A, look for a
  NEIGHBOURING article — one written by the same pipeline run, i.e. an
  adjacent created_at — whose TITLE overlaps this row's summary. A batch is 5
  consecutive new articles, so a swapped summary almost always lands on a
  neighbour. `swap_score` > 0 with a named `best_match_url` is the smoking
  gun; those rows are worth re-summarizing. A high `swap_score` on a row whose
  own title genuinely shares no words with its summary is still a judgement
  call — hence "candidates", not "hits".

Output is a CSV for hand-triage. Add a `verdict` column in the spreadsheet,
then feed the confirmed rows to a re-summarization pass (not written yet —
the honest sequencing is triage first, repair second).

Examples:
  ./.venv/bin/python3 scripts/find_misaligned_summaries.py --days 7
  ./.venv/bin/python3 scripts/find_misaligned_summaries.py --days 30 --out /tmp/candidates.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402

# Small, deliberately conservative list. Anything borderline is left IN as a
# content word — a false "has overlap" only costs a missed candidate, while an
# over-aggressive stoplist inflates the flag count with noise.
STOPWORDS = {
    "a", "about", "after", "against", "all", "an", "and", "any", "are", "as", "at",
    "be", "been", "before", "being", "but", "by", "can", "could", "did", "do", "does",
    "during", "for", "from", "had", "has", "have", "he", "her", "his", "how", "i",
    "if", "in", "into", "is", "it", "its", "just", "may", "more", "most", "new", "no",
    "not", "of", "off", "on", "one", "only", "or", "other", "our", "out", "over",
    "said", "says", "she", "should", "so", "some", "such", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "those", "through",
    "to", "two", "up", "was", "we", "were", "what", "when", "where", "which", "while",
    "who", "why", "will", "with", "would", "you", "your",
}

TOKEN_RE = re.compile(r"[a-z0-9']+")


def content_words(text: str) -> set[str]:
    """Lowercased, de-pluralized content words. Crude stemming on purpose —
    matching "senators" to "senator" matters more here than linguistic rigor."""
    words = set()
    for token in TOKEN_RE.findall((text or "").lower()):
        token = token.strip("'")
        if len(token) < 3 or token in STOPWORDS:
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        words.add(token)
    return words


async def _connect() -> asyncpg.Pool:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    return await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)


async def find_candidates(pool: asyncpg.Pool, days: int, neighbours: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT source_url, source_name, category, title, summary, created_at
          FROM articles
         WHERE summary IS NOT NULL AND summary <> ''
           AND title IS NOT NULL AND title <> ''
           AND created_at > NOW() - ($1 || ' days')::interval
         ORDER BY created_at
        """,
        str(days),
    )
    print(f"scanned {len(rows)} rows over the last {days} day(s)")

    title_words = [content_words(r["title"]) for r in rows]
    summary_words = [content_words(r["summary"]) for r in rows]

    candidates: list[dict] = []
    for i, row in enumerate(rows):
        if title_words[i] & summary_words[i]:
            continue  # title and summary share something — not a candidate

        # Detector B: does a neighbour's title explain this summary? Rows are
        # ordered by created_at, and a batch is BATCH_SIZE consecutive new
        # articles from one run, so the culprit is nearby.
        best_score = 0
        best_url = ""
        best_title = ""
        lo = max(0, i - neighbours)
        hi = min(len(rows), i + neighbours + 1)
        for j in range(lo, hi):
            if j == i:
                continue
            score = len(title_words[j] & summary_words[i])
            if score > best_score:
                best_score = score
                best_url = rows[j]["source_url"]
                best_title = rows[j]["title"]

        candidates.append({
            "source_url": row["source_url"],
            "source_name": row["source_name"],
            "category": row["category"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else "",
            "title": row["title"],
            "summary": row["summary"],
            "swap_score": best_score,
            "best_match_url": best_url,
            "best_match_title": best_title,
            "verdict": "",  # fill in by hand: real / oblique-headline / unsure
        })

    return candidates


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    parser.add_argument(
        "--neighbours", type=int, default=5,
        help="rows either side to test as the true owner of the summary (default 5, ~one batch)",
    )
    parser.add_argument("--out", default="data/misaligned_candidates.csv", help="CSV output path")
    args = parser.parse_args()

    pool = await _connect()
    try:
        candidates = await find_candidates(pool, args.days, args.neighbours)
    finally:
        await pool.close()

    strong = [c for c in candidates if c["swap_score"] >= 2]
    print(f"{len(candidates)} zero-overlap candidates (detector A — noisy, includes oblique headlines)")
    print(f"{len(strong)} of those have a neighbour whose title explains the summary (swap_score >= 2)")
    print("Neither number is the bug rate. Triage the CSV by hand.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(candidates[0].keys()) if candidates else ["source_url"])
        writer.writeheader()
        # Strongest evidence first so triage starts where the signal is.
        writer.writerows(sorted(candidates, key=lambda c: -c["swap_score"]))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
