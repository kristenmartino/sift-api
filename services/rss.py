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

# All RSS feeds — category is assigned by AI during summarization
FEEDS: list[tuple[str, str]] = [
    # ── General / Wire services ──────────────────────────
    ("AP News", "https://apnews.com/world-news.rss"),
    ("Reuters", "https://openrss.org/feed/www.reuters.com"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("BBC", "http://feeds.bbci.co.uk/news/rss.xml"),
    ("Axios", "https://api.axios.com/feed/"),
    ("The Hill", "https://thehill.com/news/feed/"),
    ("Politico", "https://rss.politico.com/politics-news.xml"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("The Guardian US", "https://www.theguardian.com/us-news/rss"),
    ("USA Today", "http://rss.usatoday.com/usatoday-newstopstories"),
    ("ABC News", "https://abcnews.go.com/abcnews/topstories"),
    ("CBS News", "https://www.cbsnews.com/latest/rss/main"),
    ("New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("Washington Post", "https://feeds.washingtonpost.com/rss/national"),
    # ── Technology ───────────────────────────────────────
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
    ("9to5Mac", "https://9to5mac.com/feed/"),
    ("9to5Google", "https://9to5google.com/feed/"),
    ("Android Central", "https://www.androidcentral.com/feed"),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
    ("TechMeme", "https://www.techmeme.com/feed.xml"),
    ("Decrypt", "https://decrypt.co/feed"),
    # ── Business & Finance ───────────────────────────────
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "https://www.marketwatch.com/rss/topstories"),
    ("Reuters Business", "https://openrss.org/feed/www.reuters.com/business"),
    ("Financial Times", "https://www.ft.com/rss/home"),
    ("The Economist", "https://www.economist.com/finance-and-economics/rss.xml"),
    ("Fortune", "https://fortune.com/feed/fortune-feeds/?id=3230629"),
    ("Business Insider", "https://feeds2.feedburner.com/businessinsider"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Quartz", "https://qz.com/feed"),
    ("Forbes", "https://www.forbes.com/innovation/feed2"),
    # ── Science ──────────────────────────────────────────
    ("Nature", "https://www.nature.com/nature.rss"),
    ("Science (AAAS)", "https://www.science.org/rss/news_current.xml"),
    ("Phys.org", "https://phys.org/rss-feed/"),
    ("New Scientist", "https://www.newscientist.com/feed/home/"),
    ("Scientific American", "http://rss.sciam.com/ScientificAmerican-Global"),
    ("Live Science", "https://www.livescience.com/feeds.xml"),
    ("Space.com", "https://www.space.com/feeds.xml"),
    ("ArXiv AI", "https://rss.arxiv.org/rss/cs.AI"),
    ("NASA", "https://www.nasa.gov/rss/dyn/breaking_news.rss"),
    ("Smithsonian", "https://www.smithsonianmag.com/rss/science-nature/"),
    ("Quanta Magazine", "https://api.quantamagazine.org/feed/"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
    ("Science News", "https://www.sciencenews.org/feed"),
    ("Undark", "https://undark.org/feed/"),
    ("Medical Xpress", "https://medicalxpress.com/rss-feed/"),
    # ── Energy & Climate ─────────────────────────────────
    ("Utility Dive", "https://www.utilitydive.com/feeds/news/"),
    ("Solar Power World", "https://www.solarpowerworldonline.com/feed/"),
    ("Renewable Energy World", "https://www.renewableenergyworld.com/feed/"),
    ("E&E News", "https://www.eenews.net/feed/"),
    ("Canary Media", "https://www.canarymedia.com/rss.rss"),
    ("CleanTechnica", "https://cleantechnica.com/feed/"),
    ("Electrek", "https://electrek.co/feed/"),
    ("Carbon Brief", "https://www.carbonbrief.org/feed"),
    ("Greentech Media", "https://www.greentechmedia.com/feed"),
    ("Energy Monitor", "https://www.energymonitor.ai/feed/"),
    ("Grist", "https://grist.org/feed/"),
    ("Inside Climate News", "https://insideclimatenews.org/feed/"),
    # ── World & Geopolitics ──────────────────────────────
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian World", "https://www.theguardian.com/world/rss"),
    ("DW News", "https://rss.dw.com/rdf/rss-en-top"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ("Foreign Policy", "https://foreignpolicy.com/feed/"),
    ("The Diplomat", "https://thediplomat.com/feed/"),
    ("South China Morning Post", "https://www.scmp.com/rss/91/feed"),
    ("Japan Times", "https://www.japantimes.co.jp/feed/"),
    ("Reuters World", "https://openrss.org/feed/www.reuters.com/world"),
    ("ABC Australia", "https://www.abc.net.au/news/feed/51120/rss.xml"),
    ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
    ("Politico EU", "https://www.politico.eu/feed/"),
    ("Defense One", "https://www.defenseone.com/rss/"),
    ("The Intercept", "https://theintercept.com/feed/?rss"),
    # ── Health & Medicine ────────────────────────────────
    ("STAT News", "https://www.statnews.com/feed/"),
    ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
    ("WHO", "https://www.who.int/rss-feeds/news-english.xml"),
    ("Health Affairs", "https://www.healthaffairs.org/action/showFeed?type=etoc&feed=rss&jc=hlthaff"),
    ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss/xml"),
    ("CDC MMWR", "https://tools.cdc.gov/api/v2/resources/media/342778.rss"),
    ("Medscape", "https://www.medscape.com/cx/rssfeeds/2700.xml"),
    ("The BMJ", "https://www.bmj.com/rss/recent.xml"),
    ("NIH News", "https://www.nih.gov/news-releases/feed.xml"),
    ("The Lancet", "https://www.thelancet.com/rssfeed/lancet_current.xml"),
    ("Healio", "https://www.healio.com/rss"),
    # ── Politics ────────────────────────────────────────
    ("Politico", "https://rss.politico.com/congress.xml"),
    ("The Hill Politics", "https://thehill.com/homenews/feed/"),
    ("RealClearPolitics", "https://feeds.feedburner.com/realclearpolitics/qlMj"),
    ("FiveThirtyEight", "https://fivethirtyeight.com/features/feed/"),
    ("Roll Call", "https://www.rollcall.com/feed/"),
    ("The Dispatch", "https://thedispatch.com/feed/"),
    ("Ballot Access News", "https://ballot-access.org/feed/"),
    ("OpenSecrets", "https://www.opensecrets.org/news/feed/"),
    # ── Sports ─────────────────────────────────────────
    ("ESPN", "https://www.espn.com/espn/rss/news"),
    ("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml"),
    ("The Athletic", "https://theathletic.com/feeds/rss/news/"),
    ("CBS Sports", "https://www.cbssports.com/rss/headlines/"),
    ("Bleacher Report", "https://bleacherreport.com/articles/feed"),
    ("Sports Illustrated", "https://www.si.com/rss/si_topstories.rss"),
    ("Yahoo Sports", "https://sports.yahoo.com/rss/"),
    ("Deadspin", "https://deadspin.com/rss"),
    # ── Entertainment ──────────────────────────────────
    ("Variety", "https://variety.com/feed/"),
    ("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
    ("Deadline", "https://deadline.com/feed/"),
    ("Entertainment Weekly", "https://ew.com/feed/"),
    ("Rolling Stone", "https://www.rollingstone.com/feed/"),
    ("Pitchfork", "https://pitchfork.com/feed/feed-news/rss"),
    ("IGN", "https://feeds.feedburner.com/ign/all"),
    ("Polygon", "https://www.polygon.com/rss/index.xml"),
    # ── Additional general-interest ──────────────────────
    ("Slate", "https://slate.com/feeds/all.rss"),
    ("Vox", "https://www.vox.com/rss/index.xml"),
    ("The Atlantic", "https://www.theatlantic.com/feed/all/"),
    ("The Conversation", "https://theconversation.com/us/articles.atom"),
    ("ProPublica", "https://www.propublica.org/feeds/propublica/main"),
    ("Rest of World", "https://restofworld.org/feed/"),
]

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


async def fetch_feeds() -> list[RSSArticle]:
    """Fetch all RSS feeds in parallel. Articles have category="" until AI classifies them."""
    tasks = []
    for source_name, feed_url in FEEDS:
        tasks.append(_fetch_single_feed(source_name, feed_url))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    articles = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Feed fetch failed: %s", result)
            continue
        articles.extend(result)

    logger.info("Fetched %d articles from %d feeds", len(articles), len(tasks))
    return articles


async def _fetch_single_feed(
    source_name: str,
    feed_url: str,
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

    return parse_feed(resp.content, source_name)


def parse_feed(data: bytes, source_name: str) -> list[RSSArticle]:
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
        image_url = _upgrade_image_url(_extract_image_url(entry))

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
            raw_content=raw_content,
        ))

    return articles


MIN_IMAGE_WIDTH = 300  # Drop thumbnails smaller than this


def _extract_image_url(entry) -> str | None:
    """Extract the best image URL from RSS media tags.

    Picks the largest media:content by width, skips thumbnails below
    MIN_IMAGE_WIDTH, and falls back through media:thumbnail and enclosures.
    """
    # 1. media:content — pick the widest image
    media_content = entry.get("media_content", [])
    if media_content:
        best_url = None
        best_width = 0
        for media in media_content:
            url = media.get("url", "")
            media_type = media.get("type", "")
            if not url or (media_type and not media_type.startswith("image/")):
                continue
            width = int(media.get("width", 0) or 0)
            if width >= best_width:
                best_url = url
                best_width = width
        if best_url and best_width >= MIN_IMAGE_WIDTH:
            return best_url
        if best_url and best_width == 0:
            return best_url  # No width info — keep it, let frontend decide

    # 2. media:thumbnail — skip if too small
    media_thumbnail = entry.get("media_thumbnail", [])
    if media_thumbnail:
        thumb = media_thumbnail[0]
        url = thumb.get("url", "")
        width = int(thumb.get("width", 0) or 0)
        if url and (width >= MIN_IMAGE_WIDTH or width == 0):
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


def _upgrade_image_url(url: str | None) -> str | None:
    """Upgrade known thumbnail URLs to higher resolution versions."""
    if not url:
        return None
    # Phys.org: /tmb/ → /800/
    if "scx1.b-cdn.net" in url and "/tmb/" in url:
        return url.replace("/tmb/", "/800/")
    # BBC: /standard/240/ → /standard/800/
    if "ichef.bbci.co.uk" in url and "/standard/240/" in url:
        return url.replace("/standard/240/", "/standard/800/")
    return url


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
