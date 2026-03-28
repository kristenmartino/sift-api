from __future__ import annotations

import asyncio
import ctypes
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from app.models import RSSArticle

logger = logging.getLogger("sift-api.rss")

# All 28 RSS feeds organized by category (from TECHNICAL_SPEC.md)
FEEDS: dict[str, list[tuple[str, str]]] = {
    "top": [
        ("AP News", "https://apnews.com/index.rss"),
        ("Reuters", "https://www.reuters.com/rssFeed/topNews"),
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("BBC", "http://feeds.bbci.co.uk/news/rss.xml"),
        ("Axios", "https://api.axios.com/feed/"),
        ("The Hill", "https://thehill.com/news/feed/"),
        ("Politico", "https://rss.politico.com/politics-news.xml"),
        ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ],
    "technology": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("Wired", "https://www.wired.com/feed/rss"),
        ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
        ("Hacker News", "https://hnrss.org/frontpage?points=50"),
        ("Engadget", "https://www.engadget.com/rss.xml"),
        ("ZDNet", "https://www.zdnet.com/news/rss.xml"),
        ("The Register", "https://www.theregister.com/headlines.atom"),
        ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
        ("VentureBeat", "https://venturebeat.com/feed/"),
    ],
    "business": [
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("MarketWatch", "https://www.marketwatch.com/rss/topstories"),
        ("Reuters Business", "https://www.reuters.com/rssFeed/businessNews"),
        ("Financial Times", "https://www.ft.com/rss/home"),
        ("The Economist", "https://www.economist.com/finance-and-economics/rss.xml"),
        ("Fortune", "https://fortune.com/feed/fortune-feeds/?id=3230629"),
        ("Business Insider", "https://feeds2.feedburner.com/businessinsider"),
        ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ],
    "science": [
        ("Nature", "https://www.nature.com/nature.rss"),
        ("Science (AAAS)", "https://www.science.org/rss/news_current.xml"),
        ("Phys.org", "https://phys.org/rss-feed/"),
        ("New Scientist", "https://www.newscientist.com/feed/home/"),
        ("Scientific American", "http://rss.sciam.com/ScientificAmericanGlobal"),
        ("Live Science", "https://www.livescience.com/feeds.xml"),
        ("Space.com", "https://www.space.com/feeds.xml"),
        ("ArXiv AI", "https://rss.arxiv.org/rss/cs.AI"),
    ],
    "energy": [
        ("Utility Dive", "https://www.utilitydive.com/feeds/news/"),
        ("Solar Power World", "https://www.solarpowerworldonline.com/feed/"),
        ("Renewable Energy World", "https://www.renewableenergyworld.com/feed/"),
        ("E&E News", "https://www.eenews.net/feed/"),
        ("Canary Media", "https://www.canarymedia.com/rss.rss"),
        ("CleanTechnica", "https://cleantechnica.com/feed/"),
        ("Electrek", "https://electrek.co/feed/"),
    ],
    "world": [
        ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("DW News", "https://rss.dw.com/rss/en/top"),
        ("France 24", "https://www.france24.com/en/rss"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
        ("Foreign Policy", "https://foreignpolicy.com/feed/"),
    ],
    "health": [
        ("STAT News", "https://www.statnews.com/feed/"),
        ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
        ("WHO", "https://www.who.int/rss-feeds/news-english.xml"),
        ("Health Affairs", "https://www.healthaffairs.org/action/showFeed?type=etoc&feed=rss&jc=hlthaff"),
        ("KFF Health News", "https://kffhealthnews.org/feed/"),
        ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss/xml"),
        ("CDC MMWR", "https://tools.cdc.gov/api/v2/resources/media/342778.rss"),
    ],
}

MAX_ENTRIES_PER_FEED = 10
FETCH_TIMEOUT = 10.0


def stable_hash(s: str) -> str:
    """Port of stableHash from sift/lib/utils.ts — djb2 variant with 32-bit signed overflow."""
    h = 0
    for ch in s:
        h = ctypes.c_int32((h << 5) - h + ord(ch)).value
    return _base36(abs(h))


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while n > 0:
        result = chars[n % 36] + result
        n //= 36
    return result


async def fetch_feeds(categories: list[str]) -> list[RSSArticle]:
    """Fetch RSS feeds for the given categories in parallel."""
    tasks = []
    for category in categories:
        feeds = FEEDS.get(category, [])
        for source_name, feed_url in feeds:
            tasks.append(_fetch_single_feed(source_name, feed_url, category))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    articles = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Feed fetch failed: %s", result)
            continue
        articles.extend(result)

    logger.info(
        "Fetched %d articles from %d feeds across %s",
        len(articles), len(tasks), categories,
    )
    return articles


async def _fetch_single_feed(
    source_name: str,
    feed_url: str,
    category: str,
) -> list[RSSArticle]:
    """Fetch and parse a single RSS feed."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                feed_url,
                timeout=FETCH_TIMEOUT,
                headers={"User-Agent": "Sift/2.0 (+https://siftnews.ai)"},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning("Failed to fetch %s (%s): %s", source_name, feed_url, e)
            return []

    return parse_feed(resp.content, source_name, category)


def parse_feed(data: bytes, source_name: str, category: str) -> list[RSSArticle]:
    """Parse RSS/Atom feed data into RSSArticle objects."""
    feed = feedparser.parse(data)
    articles = []

    for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
        link = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not link or not title:
            continue

        # Extract publication date
        pub_date = _parse_date(entry)

        # Extract image from RSS media tags
        image_url = _extract_image_url(entry)

        # Get raw content for summarization
        raw_content = ""
        if entry.get("summary"):
            raw_content = entry.summary
        elif entry.get("description"):
            raw_content = entry.description
        elif entry.get("content"):
            raw_content = entry.content[0].get("value", "")

        articles.append(RSSArticle(
            title=title,
            source_url=link,
            source_name=source_name,
            published_date=pub_date,
            image_url=image_url,
            category=category,
            raw_content=raw_content,
        ))

    return articles


def _extract_image_url(entry) -> str | None:
    """Extract image URL from RSS media tags, checking in priority order."""
    # 1. media:content
    media_content = entry.get("media_content", [])
    if media_content:
        for media in media_content:
            url = media.get("url", "")
            media_type = media.get("type", "")
            if url and (not media_type or media_type.startswith("image/")):
                return url

    # 2. media:thumbnail
    media_thumbnail = entry.get("media_thumbnail", [])
    if media_thumbnail:
        url = media_thumbnail[0].get("url", "")
        if url:
            return url

    # 3. enclosure
    enclosures = entry.get("enclosures", [])
    if enclosures:
        for enc in enclosures:
            enc_type = enc.get("type", "")
            if enc_type.startswith("image/"):
                url = enc.get("href", "") or enc.get("url", "")
                if url:
                    return url

    return None


def _parse_date(entry) -> datetime | None:
    """Parse the publication date from an RSS entry."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                from time import mktime
                return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
            except Exception:
                continue

    for field in ("published", "updated"):
        raw = entry.get(field, "")
        if raw:
            try:
                return parsedate_to_datetime(raw).replace(tzinfo=timezone.utc)
            except Exception:
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except Exception:
                    continue

    return None
