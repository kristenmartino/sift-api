"""Diagnose + prove the fix for sift-api#227: US-domestic stories filed into
`world`.

Over 7 days (n=819) New York Post alone filed 98 articles into `world`,
ahead of BBC World's 57 — all of it US-domestic human interest / crime with
no international angle. `services/summarizer.py` classifies category in the
same call that writes the summary, straight from `raw_content`, with no
feed-level hint. Two hypotheses to separate before touching the prompt:

  1. prompt ambiguity — "world" reads as "not-US-politics" by elimination.
  2. source skew — the offending feeds are content-mixed (no dedicated
     world/international section), and whatever cue the classifier uses is
     blind to that; genuinely homeless content lands on `world` because
     nothing else fits and the model is never told `general` is a valid
     landing spot.

Modes:
  diagnose  (default) sample `world`-tagged rows per source over the trailing
            window, judge each us-domestic vs. international with an LLM
            (Sonnet — needs stronger discrimination than the Haiku classifier
            being audited, same reasoning as services/judge.py), and report
            misfile rate against feed shape (dedicated section feed vs.
            general firehose, from services.rss.FEEDS).
  compare   live-fetch a fresh batch from the worst offenders and classify it
            twice in-memory — once with the current (pre-fix) prompt, once
            with the candidate (services.summarizer._build_prompt, post-fix)
            — no DB writes, same input both times. `raw_content` isn't
            persisted, so this is the only clean before/after: a stored-row
            re-classification would be confounded by the news having moved
            on (same limitation noted for scripts/compare_content_change.py).

Examples:
  ./.venv/bin/python3 scripts/eval_world_misfiles.py
  ./.venv/bin/python3 scripts/eval_world_misfiles.py --days 7 --limit 30 --json out.json
  ./.venv/bin/python3 scripts/eval_world_misfiles.py --mode compare --limit 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic  # noqa: E402
import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import RSSArticle  # noqa: E402
from services.index_alignment import AlignmentError, aligned_entries  # noqa: E402
from services.model_registry import resolve  # noqa: E402
from services.rss import FEEDS, _fetch_single_feed  # noqa: E402
from services.usage_tracker import log_usage  # noqa: E402

JUDGE_BATCH_SIZE = 10
JUDGE_OPERATION = "judge.batch"

# Sources named in the issue's table (sift-api#227), in the order given
# there. Anything else that shows up in the `world` window is reported too,
# just not called out by name.
NAMED_SOURCES = [
    "New York Post", "CBS News", "Washington Examiner", "BBC World",
    "Fox News", "BBC", "Bloomberg", "New York Times",
]

# Feed shape per source_name, derived from services.rss.FEEDS: a source has a
# *dedicated* section feed if any of its configured feed URLs plainly names a
# world/international section; otherwise every article it contributes -
# regardless of topic - comes through one general/homepage firehose.
_DEDICATED_WORLD_MARKERS = ("/world", "/international", "foreignpolicy.com")


def _feed_shape_by_source() -> dict[str, str]:
    shapes: dict[str, list[bool]] = {}
    for source_name, feed_url in FEEDS:
        is_dedicated = any(marker in feed_url for marker in _DEDICATED_WORLD_MARKERS)
        shapes.setdefault(source_name, []).append(is_dedicated)
    return {
        source: "dedicated" if any(flags) else "general"
        for source, flags in shapes.items()
    }


# --- LLM judge: is this article's content US-domestic or international? ---

def _build_judge_prompt(items: list[dict]) -> str:
    body = ""
    for i, it in enumerate(items, 1):
        body += f"\n{i}. TITLE: {it['title']}\n   SUMMARY: {it['summary']}\n"
    return f"""You are auditing news classification. For each item below, judge \
whether the article's own content is primarily about an event or affairs \
INSIDE the United States (US-domestic) or OUTSIDE the United States \
(international). Judge only what the title and summary describe — not the \
source outlet's nationality, not where it was published.

If the location is not stated or is ambiguous, answer "unclear".

Items:
{body}

Return a JSON array with one object per item, in the same order. Use short \
keys: i=index (1-based), loc=one of "us"|"intl"|"unclear".
[{{"i":1,"loc":"us"}}, ...]

Return ONLY the JSON array, no other text."""


def _extract_json_array(text: str) -> list[dict] | None:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return None


async def judge_locations(
    items: list[dict], *, client: anthropic.AsyncAnthropic
) -> list[str | None]:
    """Judge a list of {title, summary} dicts. Returns one of "us"/"intl"/
    "unclear"/None (unjudged, e.g. a misaligned batch) per item, aligned by
    position. Reuses services.judge's OPERATION bucket ("judge.batch") since
    this is the same class of call — an offline LLM audit, not a production
    classification path."""
    model = resolve(JUDGE_OPERATION).model
    out: list[str | None] = [None] * len(items)
    for start in range(0, len(items), JUDGE_BATCH_SIZE):
        sub = items[start : start + JUDGE_BATCH_SIZE]
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=100 + 20 * len(sub),
                messages=[{"role": "user", "content": _build_judge_prompt(sub)}],
            )
            log_usage(JUDGE_OPERATION, response, model=model)
            text = "".join(b.text for b in response.content if b.type == "text")
            parsed = _extract_json_array(text)
            if not parsed:
                continue
            entries = aligned_entries(parsed, len(sub))
            for idx, entry in entries.items():
                loc = str(entry.get("loc", "")).strip().lower()
                if loc in {"us", "intl", "unclear"}:
                    out[start + idx - 1] = loc
        except (AlignmentError, anthropic.APIError) as e:
            print(f"  judge batch at offset {start} failed: {e}", file=sys.stderr)
    return out


# --- diagnose mode -----------------------------------------------------

async def _connect() -> asyncpg.Pool:
    db_url = os.environ.get("DATABASE_URL", settings.database_url)
    ssl_mode = "require" if "neon.tech" in db_url else False
    return await asyncpg.create_pool(db_url, min_size=1, max_size=4, ssl=ssl_mode)


async def diagnose(days: int, limit: int, json_path: str | None) -> None:
    pool = await _connect()
    try:
        volume_rows = await pool.fetch(
            "SELECT source_name, COUNT(*) AS n FROM articles "
            "WHERE category = 'world' AND from_search = false "
            "AND published_date >= now() - ($1 || ' days')::interval "
            "GROUP BY source_name ORDER BY n DESC",
            str(days),
        )
        volume = {r["source_name"]: r["n"] for r in volume_rows}
        total = sum(volume.values())
        print(f"\n=== `world` volume, trailing {days}d (n={total}) ===")
        for source, n in list(volume.items())[:15]:
            print(f"  {source:<24} {n:>4}")

        shapes = _feed_shape_by_source()
        sources_to_sample = list(dict.fromkeys(NAMED_SOURCES + list(volume.keys())[:10]))
        sources_to_sample = [s for s in sources_to_sample if s in volume]

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
        per_source: dict[str, dict] = {}
        for source in sources_to_sample:
            rows = await pool.fetch(
                "SELECT source_url, title, summary FROM articles "
                "WHERE category = 'world' AND from_search = false AND source_name = $1 "
                "AND published_date >= now() - ($2 || ' days')::interval "
                "AND summary IS NOT NULL AND summary <> '' "
                "ORDER BY published_date DESC LIMIT $3",
                source, str(days), limit,
            )
            if not rows:
                continue
            items = [{"title": r["title"], "summary": r["summary"]} for r in rows]
            locations = await judge_locations(items, client=client)
            judged = [loc for loc in locations if loc is not None]
            us_count = sum(1 for loc in judged if loc == "us")
            per_source[source] = {
                "feed_shape": shapes.get(source, "unknown"),
                "n_world_window": volume[source],
                "n_sampled": len(rows),
                "n_judged": len(judged),
                "n_us_domestic": us_count,
                "misfile_rate": round(us_count / len(judged), 3) if judged else None,
            }
            print(
                f"  judged {source:<24} shape={shapes.get(source, '?'):<9} "
                f"sampled={len(rows):<3} us={us_count}/{len(judged)}"
            )

        print("\n=== Misfile rate by source (sampled, LLM-judged) ===")
        print(f"  {'source':<24} {'feed shape':<11} {'n 7d':>6} {'sampled':>8} {'misfile %':>10}")
        for source, stats in per_source.items():
            rate = "n/a" if stats["misfile_rate"] is None else f"{stats['misfile_rate']*100:.0f}%"
            print(
                f"  {source:<24} {stats['feed_shape']:<11} {stats['n_world_window']:>6} "
                f"{stats['n_sampled']:>8} {rate:>10}"
            )

        general_rates = [s["misfile_rate"] for s in per_source.values() if s["feed_shape"] == "general" and s["misfile_rate"] is not None]
        dedicated_rates = [s["misfile_rate"] for s in per_source.values() if s["feed_shape"] == "dedicated" and s["misfile_rate"] is not None]
        print("\n=== Hypothesis verdict ===")
        if general_rates:
            print(f"  general-firehose sources: mean misfile rate = {sum(general_rates)/len(general_rates)*100:.1f}% (n={len(general_rates)} sources)")
        if dedicated_rates:
            print(f"  dedicated-feed sources:   mean misfile rate = {sum(dedicated_rates)/len(dedicated_rates)*100:.1f}% (n={len(dedicated_rates)} sources)")

        if json_path:
            with open(json_path, "w") as f:
                json.dump({"days": days, "volume": dict(volume), "per_source": per_source}, f, indent=2, default=str)
            print(f"\nWrote {json_path}")
    finally:
        await pool.close()


# --- compare mode: live-fetch + dual-prompt, no DB writes ----------------

async def compare(sources: list[str], limit: int, json_path: str | None) -> None:
    from services.summarizer import (  # noqa: PLC0415
        FALLBACK_CATEGORY,
        _build_prompt as build_prompt_candidate,
        _extract_json_array as summarizer_extract_json_array,
        _has_summarizable_content,
    )

    feeds_by_source = {name: url for name, url in FEEDS if name in sources}
    missing = set(sources) - set(feeds_by_source)
    if missing:
        print(f"Unknown source(s), skipping: {missing}", file=sys.stderr)

    all_articles: list[RSSArticle] = []
    for source_name, feed_url in feeds_by_source.items():
        result = await _fetch_single_feed(source_name, feed_url)
        all_articles.extend(result.articles[:limit])

    all_articles = [a for a in all_articles if _has_summarizable_content(a)]
    print(f"Fetched {len(all_articles)} summarizable articles live from {list(feeds_by_source)}")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    async def classify(prompt_fn, batch: list[RSSArticle]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(batch), 5):
            sub = batch[i : i + 5]
            prompt = prompt_fn(sub)
            spec = resolve("summarizer.batch")
            response = await client.messages.create(
                model=spec.model, max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            log_usage("summarizer.batch", response, model=spec.model)
            text = "".join(b.text for b in response.content if b.type == "text")
            parsed = summarizer_extract_json_array(text)
            if not parsed:
                continue
            try:
                entries = aligned_entries(parsed, len(sub))
            except AlignmentError as e:
                print(f"  batch at offset {i} misaligned: {e}", file=sys.stderr)
                continue
            for idx, entry in entries.items():
                category = str(entry.get("c", entry.get("category", FALLBACK_CATEGORY))).strip().lower()
                out[sub[idx - 1].source_url] = category
        return out

    before = await classify(_build_prompt_baseline, all_articles)
    after = await classify(build_prompt_candidate, all_articles)

    def _dist(mapping: dict[str, str]) -> dict[str, int]:
        d: dict[str, int] = {}
        for cat in mapping.values():
            d[cat] = d.get(cat, 0) + 1
        return d

    before_dist, after_dist = _dist(before), _dist(after)
    print("\n=== category distribution, same live batch, before vs. after prompt ===")
    cats = sorted(set(before_dist) | set(after_dist))
    for cat in cats:
        print(f"  {cat:<14} before={before_dist.get(cat, 0):>4}  after={after_dist.get(cat, 0):>4}")

    moved_out_of_world = {
        url: after[url] for url in before
        if before.get(url) == "world" and after.get(url) != "world"
    }
    print(f"\nArticles that moved OUT of `world`: {len(moved_out_of_world)}/{before_dist.get('world', 0)}")
    for url, new_cat in list(moved_out_of_world.items())[:10]:
        print(f"  -> {new_cat:<10} {url}")

    if json_path:
        with open(json_path, "w") as f:
            json.dump({"before": before, "after": after, "before_dist": before_dist, "after_dist": after_dist}, f, indent=2)
        print(f"\nWrote {json_path}")


def _build_prompt_baseline(batch: list[RSSArticle]) -> str:
    """Frozen copy of services.summarizer._build_prompt as it stood BEFORE
    sift-api#227's fix, for before/after comparison purposes only. Kept as a
    literal string (not imported) because the whole point is to diff against
    what production is doing *today*; once the fix lands this is deliberately
    stale and should not be "helpfully" kept in sync."""
    articles_text = ""
    for i, article in enumerate(batch, 1):
        content = article.raw_content or article.title
        content = re.sub(r"<[^>]+>", "", content).strip()
        words = content.split()
        content = content if len(words) <= 500 else " ".join(words[:500]) + "..."
        articles_text += f"\n{i}. Title: {article.title}\n   Source: {article.source_name}\n   Content: {content}\n"

    return f"""Summarize each of the following news articles in 1-2 concise sentences. Focus on the key facts and why the story matters.

Also classify each article into exactly ONE category:
- "top" — reserved for stories of major, broad public significance: breaking news of national or international consequence, or stories that genuinely cut across several of the categories below. A story is NOT "top" merely because it is dramatic, violent, or shocking. Single-incident crime, accident, or death stories (a killing, a shooting, an assault charge, a fatal crash, a trial) are never "top" — classify them into the closest topical category instead
- "technology" — tech industry, software, hardware, AI, cybersecurity, social media
- "business" — Wall Street, stock market, earnings reports, M&A, IPOs, venture capital, interest rates, Federal Reserve, banking, employment data, GDP, inflation, corporate strategy, trade policy. NOT consumer product launches, pop culture brands, or retail sales events
- "science" — research, discoveries, space, physics, biology, climate science
- "energy" — power grid, renewables, oil & gas, EVs, energy policy, utilities
- "world" — international affairs, geopolitics, diplomacy, foreign policy
- "health" — medicine, public health, pharma, healthcare policy, disease
- "politics" — elections, legislation, political parties, Congress, campaigns, government policy
- "sports" — professional sports, college sports, Olympics, player trades, game results
- "entertainment" — movies, TV, music, celebrities, streaming, awards, pop culture, consumer product launches, brand collaborations, viral consumer trends

Most articles must go into a specific topic category — when unsure, prefer the specific category over "top".

Routing rule for crime, accident, and death stories: these are "top" only when \
the event itself has national or international consequence (a mass-casualty \
attack, the assassination of a public figure, a disaster prompting a national \
response). Otherwise classify by setting or participants: sports figures → \
"sports"; entertainers or media personalities → "entertainment"; corporate or \
financial wrongdoing → "business"; policing, courts, or justice-system policy → \
"politics"; incidents outside the U.S. → "world"; deaths with public-health \
relevance (overdoses, outbreaks) → "health". If none fit, pick the closest \
category anyway — never default to "top".

Rules about people and legal matters — these override everything above:
- Describe only what the source article states. Do not add facts, motives, or \
history that are not in the text you were given.
- Never characterize a legal outcome beyond what the source literally says. \
"Charged" is not "guilty." "Settled" is not "found liable." An investigation is \
not a finding. An accusation is not a fact.
- Attribute contested claims to whoever made them: "prosecutors say," "the \
complaint alleges," "according to <outlet>."
- A campaign contribution is not an endorsement. A committee seat is not a \
position. A vote is not a belief.
- If the article's own framing is uncertain, keep the uncertainty. Do not \
resolve it.

{articles_text}

Return a JSON array with one object per article, in the same order.
Use short keys: i=index, s=summary, c=category.
[{{"i":1,"s":"1-2 sentence summary","c":"technology"}}, ...]

Return ONLY the JSON array, no other text."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["diagnose", "compare"], default="diagnose")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--sources", nargs="*", default=["New York Post", "CBS News", "Washington Examiner", "Fox News"])
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    if args.mode == "diagnose":
        asyncio.run(diagnose(args.days, args.limit, args.json_path))
    else:
        asyncio.run(compare(args.sources, args.limit, args.json_path))


if __name__ == "__main__":
    main()
