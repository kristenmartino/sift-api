#!/usr/bin/env python3
"""Same article, same model — old input vs new input.

WHY THIS SHAPE
--------------
#240 changed which RSS field the summarizer reads. The obvious check is to diff
stored summaries before and after the deploy, and it does not work: `articles`
does not persist `raw_content`, and the rows after a deploy are DIFFERENT
articles, so any difference is confounded with the news changing.

So this holds everything constant except the one thing that changed. It fetches
live feeds, reconstructs BOTH inputs for the same entry — the teaser
`parse_feed` used to read, and the body it reads now — and summarizes each with
the production prompt and parser. The only variable is the input.

Only entries where the two inputs differ are sampled: the other ~75% are
unaffected by #240 and would pad the sample with identical pairs.

Usage:
    ./.venv/bin/python3 scripts/compare_content_change.py --n 20
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic  # noqa: E402
import feedparser  # noqa: E402
import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import RSSArticle  # noqa: E402
from services import summarizer  # noqa: E402
from services.model_registry import resolve  # noqa: E402
from services.rss import FEEDS, FETCH_TIMEOUT, _best_content  # noqa: E402


def _old_content(entry) -> str:
    """What parse_feed read before #240: first match wins."""
    if entry.get("summary"):
        return entry.summary
    if entry.get("description"):
        return entry.description
    blocks = entry.get("content") or []
    return (blocks[0] or {}).get("value", "") if blocks else ""


def _words(html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", html or "").split())


async def _fetch(client, name, url):
    try:
        r = await client.get(
            url, timeout=FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SiftBot/1.0)"},
        )
        return name, feedparser.parse(r.content)
    except Exception:
        return name, None


async def collect(n: int) -> list[tuple[str, str, str, str]]:
    async with httpx.AsyncClient(follow_redirects=True) as c:
        res = await asyncio.gather(*(_fetch(c, n_, u) for n_, u in FEEDS))

    by_source: dict[str, list] = defaultdict(list)
    for name, feed in res:
        if not feed:
            continue
        for e in feed.entries[:10]:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            old, new = _old_content(e), _best_content(e)
            if old == new:
                continue  # unaffected by #240
            by_source[name].append((name, title, old, new))

    # Round-robin across outlets so one prolific feed does not fill the sample.
    picked, rnd = [], 0
    while len(picked) < n:
        added = False
        for src in sorted(by_source):
            if rnd < len(by_source[src]) and len(picked) < n:
                picked.append(by_source[src][rnd])
                added = True
        if not added:
            break
        rnd += 1
    return picked


async def summarize(client, items: list[tuple[str, str, str]]) -> dict[str, dict]:
    """Production prompt + production parser, one batch at a time."""
    out: dict[str, dict] = {}
    arts = [
        RSSArticle(title=t, source_url=f"https://x/{i}", source_name=s,
                   raw_content=body)
        for i, (s, t, body) in enumerate(items)
    ]
    spec = resolve("summarizer.batch")
    for i in range(0, len(arts), summarizer.BATCH_SIZE):
        batch = arts[i : i + summarizer.BATCH_SIZE]
        resp = await client.messages.create(
            model=spec.model,
            max_tokens=summarizer.MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": summarizer._build_prompt(batch)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            out.update(summarizer._parse_summaries(text, batch))
        except Exception as e:
            print(f"    batch {i // summarizer.BATCH_SIZE} failed: {e}",
                  file=sys.stderr)
    return out


async def main(n: int) -> int:
    picked = await collect(n)
    if not picked:
        print("No entries where #240 changed the input. Nothing to compare.")
        return 0
    print(f"  {len(picked)} articles where #240 changed the summarizer's input\n")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key,
                                      max_retries=2)
    old_items = [(s, t, old) for s, t, old, _ in picked]
    new_items = [(s, t, new) for s, t, _, new in picked]
    old_res, new_res = await asyncio.gather(
        summarize(client, old_items), summarize(client, new_items)
    )

    for i, (src, title, old, new) in enumerate(picked):
        url = f"https://x/{i}"
        o, nw = old_res.get(url), new_res.get(url)
        if not o or not nw:
            continue
        print("=" * 78)
        print(f"{i + 1}. [{src}] {title}")
        print(f"   input: {_words(old)} words -> {_words(new)} words")
        print(f"\n   BEFORE ({o['category']}):\n     {o['summary']}")
        print(f"\n   AFTER  ({nw['category']}):\n     {nw['summary']}")
        print()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=20)
    sys.exit(asyncio.run(main(p.parse_args().n)))
