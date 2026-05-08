from __future__ import annotations

import asyncio
import calendar
import ctypes
import hashlib
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from app.models import RSSArticle

logger = logging.getLogger("sift-api.rss")

# RSS feeds — Phase 2.0 (civic-literacy MVP).
#
# Pruned from ~110 long-tail feeds to ~50 outlets that map to a curated
# outlet_profiles whitelist (forthcoming in Phase 2.A). The trim aligns
# Sift's marketing claim ("vetted outlets across the political spectrum")
# with what we actually ingest from. See plans/sift-civic-literacy.md and
# plans/sift-phase-2-cross-spectrum-and-outlet-provenance.md.
#
# Inclusion criteria for the curated set:
#   • MBFC factual-accuracy rating ≥ "Mixed" (we exclude "Low" / "Very Low")
#   • Identifiable masthead, corrections policy, and bylines
#   • AllSides-rated when applicable; symmetric L/C/R representation
#   • Specialty outlets (Nature, Bloomberg, etc.) included when they
#     dominate a sector regardless of bias-spectrum positioning
#
# Categories sports + entertainment fall *outside* the civic-literacy
# framework — their feeds are kept here as fallback content so the
# corresponding /news category tabs don't empty out, but those articles
# do not get cross-spectrum or outlet-provenance treatments. Whether
# those categories survive long-term is a separate product call.
#
# Right-leaning outlets we have curated in `outlet_profiles` but without
# a working RSS endpoint:
#   - The American Conservative (theamericanconservative.com/feed/) → 403
#     Cloudflare bot-protect even with browser UA. No public alternative.
#   - The Federalist (thefederalist.com/feed/) → 403 same as above.
# Both have outlet_profiles rows so any direct mention via entity-linker
# still resolves to a dossier; we just won't ingest their headlines.

FEEDS: list[tuple[str, str]] = [
    # ── General / Wire services ──────────────────────────
    # AP News and Reuters retired their public RSS feeds (AP behind an
    # auth-walled API; Reuters killed RSS in 2020). The openrss.org
    # wrapper for Reuters is now rate-limiting us (HTTP 429). Wire copy
    # from both still reaches Sift via NPR / CBS / USA Today / ABC /
    # BBC / Guardian, all of which run AP and Reuters syndication.
    # TODO: revisit if/when we acquire AP API or Reuters Connect access.
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
    # WaPo: /rss/national was returning ~5 items (and intermittently empty
    # on Railway egress); /rss/homepage is the full firehose (~70 items).
    ("Washington Post", "https://feeds.washingtonpost.com/rss/homepage"),
    # Fox News' Google Publisher Center feed — Atom-style, ~25 entries
    # per fetch. Their direct RSS at /rss/* is paywalled/auth-walled.
    ("Fox News", "https://moxie.foxnews.com/google-publisher/latest.xml"),
    ("New York Post", "https://nypost.com/feed/"),
    # ── Technology (specialty) ───────────────────────────
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    # ── Business & Finance ───────────────────────────────
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Financial Times", "https://www.ft.com/rss/home"),
    ("The Economist", "https://www.economist.com/finance-and-economics/rss.xml"),
    ("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Forbes", "https://www.forbes.com/innovation/feed2"),
    # ── Science (specialty) ──────────────────────────────
    ("Nature", "https://www.nature.com/nature.rss"),
    ("Science (AAAS)", "https://www.science.org/rss/news_current.xml"),
    # ── Energy & Climate ─────────────────────────────────
    ("Canary Media", "https://www.canarymedia.com/rss.rss"),
    ("Carbon Brief", "https://www.carbonbrief.org/feed"),
    ("Inside Climate News", "https://insideclimatenews.org/feed/"),
    # ── World & Geopolitics ──────────────────────────────
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("The Guardian World", "https://www.theguardian.com/world/rss"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ("Foreign Policy", "https://foreignpolicy.com/feed/"),
    ("The Intercept", "https://theintercept.com/feed/?rss"),
    # ── Health & Medicine ────────────────────────────────
    ("STAT News", "https://www.statnews.com/feed/"),
    ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
    ("WHO", "https://www.who.int/rss-feeds/news-english.xml"),
    # ── Politics ─────────────────────────────────────────
    ("Politico Congress", "https://rss.politico.com/congress.xml"),
    ("The Hill Politics", "https://thehill.com/homenews/feed/"),
    ("The Dispatch", "https://thedispatch.com/feed/"),
    # Right-leaning political outlets — the corpus before this addition
    # had ~0 articles/week from any "right" or "lean-right" outlet
    # despite the home page promising "across the political spectrum".
    # All eight of these returned HTTP 200 with non-empty bodies under
    # the Sift/1.0 user-agent during pre-merge curl audit.
    ("Reason", "https://reason.com/latest/feed/"),
    ("National Review", "https://www.nationalreview.com/feed/"),
    ("Washington Examiner", "https://www.washingtonexaminer.com/feed"),
    ("The Washington Times", "https://www.washingtontimes.com/rss/headlines/news/national/"),
    ("The Daily Caller", "https://dailycaller.com/feed/"),
    ("The Daily Wire", "https://www.dailywire.com/feeds/rss.xml"),
    # ── Sports (out-of-scope for civic-literacy mechanics) ──
    ("ESPN", "https://www.espn.com/espn/rss/news"),
    ("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml"),
    ("CBS Sports", "https://www.cbssports.com/rss/headlines/"),
    # SI moved the feed; old /rss/si_topstories.rss was 404.
    ("Sports Illustrated", "https://www.si.com/feed"),
    # ── Entertainment (out-of-scope for civic-literacy mechanics) ──
    ("Variety", "https://variety.com/feed/"),
    ("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
    ("Deadline", "https://deadline.com/feed/"),
    ("Rolling Stone", "https://www.rollingstone.com/feed/"),
    ("Pitchfork", "https://pitchfork.com/feed/feed-news/rss"),
    # ── Additional general-interest ──────────────────────
    ("Slate", "https://slate.com/feeds/all.rss"),
    ("Vox", "https://www.vox.com/rss/index.xml"),
    ("The Atlantic", "https://www.theatlantic.com/feed/all/"),
    ("ProPublica", "https://www.propublica.org/feeds/propublica/main"),
]

MAX_ENTRIES_PER_FEED = 10
FETCH_TIMEOUT = 10.0


def stable_hash(s: str) -> str:
    """Port of stableHash from sift/lib/utils.ts — djb2 variant with 32-bit signed overflow."""
    h = 0
    for ch in s:
        h = ctypes.c_int32((h << 5) - h + ord(ch)).value
    return _base36(abs(h))


_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
CONTENT_HASH_PREFIX_CHARS = 500


def compute_content_hash(title: str, raw_content: str) -> str:
    """SHA-256 of normalized title + content prefix, used for pre-Claude dedup.

    Normalization: strip HTML, lowercase, collapse runs of whitespace, trim.
    This catches syndicated copies of the same story across feeds (AP → NPR,
    Yahoo, ABC, etc.) whose source_url differs but body text is identical.
    """
    def _norm(text: str) -> str:
        text = _TAG_RE.sub(" ", text or "")
        text = _WS_RE.sub(" ", text).strip().lower()
        return text

    t = _norm(title)
    c = _norm(raw_content)[:CONTENT_HASH_PREFIX_CHARS]
    return hashlib.sha256(f"{t}\n{c}".encode("utf-8")).hexdigest()


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
                headers={"User-Agent": "Sift/1.0 (+https://siftnews.kristenmartino.ai)"},
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
            content_hash=compute_content_hash(title, raw_content),
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
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except Exception:
                continue

    for field in ("published", "updated"):
        raw = entry.get(field, "")
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except Exception:
                    continue

    return None
