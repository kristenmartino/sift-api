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

  Detector B (the useful one): for each row flagged by A, look for another
  article from the SAME PIPELINE RUN — anything inserted within
  --window-seconds of it — whose TITLE overlaps this row's summary. A swapped
  summary belongs to one of the run's own articles, so the true owner is in
  that burst. `swap_score` >= 2 with a named `best_match_url` is the smoking
  gun.

  Beware the converse: two outlets covering the same event also score high,
  because their titles legitimately overlap each other's summaries. A high
  score means "look at this", not "this is broken".

  This started as a +/-5-row scan, which MISSED a confirmed production case:
  the true owner of the misplaced summary sat 8 rows away (verified
  2026-07-31 — the Guatemala/Blanche pair scored 0 and would have been
  triaged last). One run inserts ~60 rows in a couple of minutes and scoring
  them all is cheap, so the window is now time-based.

Output is a CSV for hand-triage. Add a `verdict` column in the spreadsheet,
then feed the confirmed rows to a re-summarization pass (not written yet —
the honest sequencing is triage first, repair second).

Examples:
  ./.venv/bin/python3 scripts/find_misaligned_summaries.py --days 7 --out data/_cache/c.csv
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

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Order matters: longest suffix first, so "communities" stems past "es".
_SUFFIXES = ("ies", "ing", "ory", "ers", "ed", "es", "s")


def content_words(text: str) -> set[str]:
    """Lowercased, stemmed content words. Crude on purpose — matching
    "senators" to "senator" matters more here than linguistic rigor.

    Three things the first version got wrong, each of which inflated the flag
    count with articles whose summary was fine (measured 2026-07-31: 186
    flagged, 123 after these fixes):

      - apostrophes were kept, so "Blanche's" never matched "Blanche";
      - a 3-character floor dropped "AI", "US", "EU" — the entire subject of
        some headlines;
      - only a trailing "s" was stripped, so "regulate"/"regulatory" and
        "protest"/"protesters" read as unrelated.
    """
    words = set()
    # Drop apostrophes before tokenizing so possessives collapse into the
    # base word ("blanche's" -> "blanches" -> "blanche").
    for token in TOKEN_RE.findall((text or "").lower().replace("'", "").replace("’", "")):
        if len(token) < 2 or token in STOPWORDS:
            continue
        for suffix in _SUFFIXES:
            if len(token) > len(suffix) + 3 and token.endswith(suffix):
                token = token[: -len(suffix)]
                break
        words.add(token)
    return words


async def _connect() -> asyncpg.Pool:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    return await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)


async def find_candidates(pool: asyncpg.Pool, days: int, window_seconds: int) -> list[dict]:
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

        # Detector B: does another article from the same pipeline run explain
        # this summary? Rows are ordered by created_at; walk outward until the
        # timestamps leave the run's insert burst.
        best_score = 0
        best_url = ""
        best_title = ""
        for step in (-1, 1):
            j = i + step
            while 0 <= j < len(rows):
                gap = abs((rows[j]["created_at"] - row["created_at"]).total_seconds())
                if gap > window_seconds:
                    break
                score = len(title_words[j] & summary_words[i])
                if score > best_score:
                    best_score = score
                    best_url = rows[j]["source_url"]
                    best_title = rows[j]["title"]
                j += step

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
        "--window-seconds", type=int, default=300,
        help="how far either side, in insert time, to look for the summary's true owner "
             "(default 300 — one pipeline run's insert burst)",
    )
    parser.add_argument(
        "--out", default="data/_cache/misaligned_candidates.csv",
        help="CSV output path. Defaults under data/_cache/, which is gitignored — "
             "the rows carry prod article text.",
    )
    args = parser.parse_args()

    pool = await _connect()
    try:
        candidates = await find_candidates(pool, args.days, args.window_seconds)
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
