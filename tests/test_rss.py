from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from services.rss import stable_hash, _base36, compute_content_hash, parse_feed, FEEDS

# Pinned goldens. `stable_hash` is a port of `stableHash` in sift/lib/utils.ts,
# and these EXACT values are asserted in sift/__tests__/utils.test.ts too. The
# two implementations must never diverge:
#
#   workflows/pipeline_workflow.py:337  article_id = stable_hash(source_url + title)
#                                       — the PRIMARY KEY of every `articles` row
#   sift/app/api/news/topic/route.ts:578 computes the SAME id in TypeScript
#
# If either implementation changes, every stored article is orphaned and id
# matching across the two repos breaks. The previous test here asserted only
# `stable_hash("hello") == stable_hash("hello")`, which cannot detect that.
GOLDEN_STABLE_HASH = {
    "hello": "1n1e4y",
    "world": "1vgtci",
    "": "0",
    "test article url": "p4glh3",
    "https://www.npr.org/2026/06/04/x": "c9vv14",
}


class TestStableHash:
    """Test the djb2 hash port from JS."""

    def test_known_values(self):
        for text, expected in GOLDEN_STABLE_HASH.items():
            assert stable_hash(text) == expected, f"stable_hash({text!r}) drifted"

    def test_int32_min_boundary(self):
        """The one input where the C-int32 wrap and JS's double-based Math.abs
        could part ways: abs(INT32_MIN) overflows int32 but is exact in a
        double. Both must render 2147483648 as base36 'zik0zk'."""
        assert _base36(2147483648) == "zik0zk"

    def test_different_inputs_differ(self):
        assert stable_hash("hello") != stable_hash("world")

    def test_empty_string(self):
        result = stable_hash("")
        assert result == "0"

    def test_returns_base36_string(self):
        result = stable_hash("test article url")
        # base36 uses only 0-9 and a-z
        assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in result)


class TestBase36:
    def test_zero(self):
        assert _base36(0) == "0"

    def test_small_numbers(self):
        assert _base36(1) == "1"
        assert _base36(10) == "a"
        assert _base36(35) == "z"
        assert _base36(36) == "10"

    def test_large_number(self):
        result = _base36(123456789)
        assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in result)


class TestComputeContentHash:
    """`compute_content_hash` had no test at all, yet it drives the entire
    content_hash dedup path in services/deduplicator.py:50-56 — the thing that
    stops us paying Claude twice for the same syndicated wire story. A silent
    change here either re-ingests everything or dedups nothing.
    """

    # One golden locks all four normalization steps at once: HTML stripping,
    # whitespace collapse, lowercasing, and the 500-char prefix.
    GOLDEN = "6bb3eb2c5a45a6a3bf0650f27ab19d0a3b25e3c078d89837a9559ff64fa79409"

    def test_known_value(self):
        assert (
            compute_content_hash(
                "Fed holds rates", "<p>The Federal   Reserve held rates steady.</p>"
            )
            == self.GOLDEN
        )

    def test_html_is_stripped(self):
        assert compute_content_hash("t", "<b>hi</b>") == compute_content_hash("t", "hi")

    def test_whitespace_runs_collapse(self):
        assert compute_content_hash("t", "a    b") == compute_content_hash("t", "a b")

    def test_case_insensitive(self):
        assert compute_content_hash("Title", "Body") == compute_content_hash("title", "body")

    def test_only_the_first_500_chars_of_content_matter(self):
        base = "x" * 500
        assert compute_content_hash("t", base) == compute_content_hash("t", base + "DIVERGES")

    def test_content_differing_within_the_prefix_still_separates(self):
        assert compute_content_hash("t", "a" * 499 + "b") != compute_content_hash("t", "a" * 500)

    def test_title_is_part_of_the_hash(self):
        assert compute_content_hash("one", "body") != compute_content_hash("two", "body")

    def test_handles_none_content_without_raising(self):
        # parse_feed can hand us an entry with no body.
        assert compute_content_hash("t", "") == compute_content_hash("t", "   ")


class TestParseFeed:
    def test_parse_rss(self, sample_rss_xml):
        articles = parse_feed(sample_rss_xml, "TestSource")
        assert len(articles) == 3
        assert articles[0].title == "Test Article One"
        assert articles[0].source_url == "https://example.com/article-1"
        assert articles[0].source_name == "TestSource"
        assert articles[0].raw_content == "This is the first test article about technology."

    def test_parse_atom(self, sample_atom_xml):
        articles = parse_feed(sample_atom_xml, "AtomSource")
        assert len(articles) == 1
        assert articles[0].title == "Atom Article"
        assert articles[0].source_name == "AtomSource"

    def test_image_extraction_media_content(self, sample_rss_xml):
        articles = parse_feed(sample_rss_xml, "Test")
        # First article has media:content
        assert articles[0].image_url == "https://example.com/image1.jpg"

    def test_image_extraction_enclosure(self, sample_rss_xml):
        articles = parse_feed(sample_rss_xml, "Test")
        # Third article has enclosure
        assert articles[2].image_url == "https://example.com/image3.jpg"

    def test_no_image(self, sample_rss_xml):
        articles = parse_feed(sample_rss_xml, "Test")
        # Second article has no image
        assert articles[1].image_url is None

    def test_published_date_parsed(self, sample_rss_xml):
        articles = parse_feed(sample_rss_xml, "Test")
        assert articles[0].published_date is not None

    def test_empty_feed(self):
        articles = parse_feed(b"<rss><channel></channel></rss>", "Empty")
        assert articles == []

    def test_max_entries_limit(self):
        # Build a feed with 15 items
        items = ""
        for i in range(15):
            items += f"""<item>
                <title>Article {i}</title>
                <link>https://example.com/article-{i}</link>
                <description>Description {i}</description>
            </item>"""
        feed_xml = f"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Big Feed</title>{items}</channel></rss>"""
        articles = parse_feed(feed_xml.encode(), "Test")
        # MAX_ENTRIES_PER_FEED is 10
        assert len(articles) == 10


class TestFeedConfig:
    def test_feed_count(self):
        # Civic-literacy MVP (Phase 2.0): curated outlet whitelist.
        # Lower bound guards against accidental empty-feed deploys; upper
        # bound flags drift back into the long-tail aggregator era.
        # See plans/sift-phase-2-cross-spectrum-and-outlet-provenance.md.
        assert 40 <= len(FEEDS) <= 70, (
            f"FEEDS count ({len(FEEDS)}) is outside the curated range. "
            "Adding feeds? Confirm the outlet is on the curated list. "
            "Cutting? Confirm a category isn't left empty."
        )

    def test_feeds_are_tuples(self):
        for feed in FEEDS:
            assert isinstance(feed, tuple), f"Feed {feed} is not a tuple"
            assert len(feed) == 2, "Feed tuple should have 2 elements"
            name, url = feed
            assert isinstance(name, str)
            assert url.startswith("http")

    def test_readme_feed_count_matches(self):
        # Drift guard: the README's "NN RSS feeds" claim must match len(FEEDS).
        # Code is the single source of truth; this fails the build if the doc
        # and the feed list diverge — the exact regression that left the README
        # claiming "100+" while FEEDS held 58.
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        match = re.search(r"(\d+)\s+RSS feeds", text)
        assert match, "Could not find an 'NN RSS feeds' claim in README.md"
        assert int(match.group(1)) == len(FEEDS), (
            f"README says {match.group(1)} RSS feeds but FEEDS has {len(FEEDS)}. "
            "Update the count in sift-api/README.md to match."
        )


class TestFeedStats:
    """The `feed_stats` event (#122).

    The Washington Post feed returned HTTP 400 for ~15 days and nothing
    noticed: `_fetch_single_feed` swallowed the error and returned an empty
    list, `return_exceptions=True` hid the rest, and the run reported success.
    An empty list cannot distinguish "the fetch failed" from "the feed answered
    and had nothing" — these pin the distinction and the reporting.
    """

    @staticmethod
    def _articles(n: int, source: str) -> list:
        from app.models import RSSArticle

        return [
            RSSArticle(
                title=f"{source} {i}",
                source_url=f"https://example.com/{source}/{i}",
                source_name=source,
                raw_content="body",
            )
            for i in range(n)
        ]

    @staticmethod
    def _events(caplog) -> list[dict]:
        return [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{") and '"feed_stats"' in r.message
        ]

    async def _run(self, monkeypatch, caplog, outcome):
        """Replace the per-feed fetch, then run the real aggregation."""
        from services import rss

        async def fake(source_name, feed_url):
            return outcome(source_name, feed_url)

        monkeypatch.setattr(rss, "_fetch_single_feed", fake)
        with caplog.at_level(logging.INFO, logger="sift-api.rss"):
            articles = await rss.fetch_feeds()
        return articles, self._events(caplog)

    @pytest.mark.asyncio
    async def test_healthy_run_reports_every_feed_ok(self, monkeypatch, caplog):
        from services.rss import FeedResult

        articles, events = await self._run(
            monkeypatch, caplog,
            lambda s, u: FeedResult(s, u, self._articles(2, s)),
        )
        assert len(articles) == 2 * len(FEEDS)
        assert events, "no feed_stats event was emitted"
        stats = events[0]
        assert stats["feeds_total"] == len(FEEDS)
        assert stats["feeds_ok"] == len(FEEDS)
        assert stats["feeds_failed"] == 0
        assert stats["feeds_empty"] == 0
        assert stats["articles"] == 2 * len(FEEDS)

    @pytest.mark.asyncio
    async def test_http_error_is_named_not_swallowed(self, monkeypatch, caplog):
        """The #122 case: one feed 400s and the run still succeeds."""
        from services.rss import FeedResult

        dead = FEEDS[0][0]

        def outcome(s, u):
            if s == dead:
                return FeedResult(s, u, [], "HTTPStatusError: 400 Bad Request")
            return FeedResult(s, u, self._articles(1, s))

        articles, events = await self._run(monkeypatch, caplog, outcome)

        assert len(articles) == len(FEEDS) - 1  # the run still produces
        stats = events[0]
        assert stats["feeds_failed"] == 1
        assert stats["failed"][0]["source"] == dead
        assert "400" in stats["failed"][0]["error"]
        assert stats["articles_by_source"][dead] == 0
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_feed_that_answers_with_nothing_is_distinguished_from_one_that_errored(
        self, monkeypatch, caplog
    ):
        from services.rss import FeedResult

        quiet = FEEDS[1][0]

        def outcome(s, u):
            if s == quiet:
                return FeedResult(s, u, [])  # HTTP 200, zero usable entries
            return FeedResult(s, u, self._articles(1, s))

        _, events = await self._run(monkeypatch, caplog, outcome)
        stats = events[0]
        assert stats["feeds_empty"] == 1
        assert stats["empty"] == [quiet]
        assert stats["feeds_failed"] == 0, "an empty feed is not a failed feed"

    @pytest.mark.asyncio
    async def test_a_raised_exception_is_attributed_to_the_right_feed(self, monkeypatch, caplog):
        """gather(return_exceptions=True) loses the feed identity unless the
        results are paired back against FEEDS in order."""
        from services.rss import FeedResult

        boom = FEEDS[2][0]

        def outcome(s, u):
            if s == boom:
                raise RuntimeError("connection reset")
            return FeedResult(s, u, self._articles(1, s))

        _, events = await self._run(monkeypatch, caplog, outcome)
        stats = events[0]
        assert stats["feeds_failed"] == 1
        assert stats["failed"][0]["source"] == boom
        assert "connection reset" in stats["failed"][0]["error"]

    @pytest.mark.asyncio
    async def test_every_configured_source_appears_in_the_counts(self, monkeypatch, caplog):
        """A dying feed has to show up as a zero, not as an absent key."""
        from services.rss import FeedResult

        _, events = await self._run(
            monkeypatch, caplog, lambda s, u: FeedResult(s, u, []),
        )
        counts = events[0]["articles_by_source"]
        assert set(counts) == {name for name, _ in FEEDS}
        assert set(counts.values()) == {0}

    @pytest.mark.asyncio
    async def test_fetch_failure_carries_the_error_out_instead_of_returning_bare_empty(
        self, monkeypatch
    ):
        """_fetch_single_feed's own contract — the half that used to swallow."""
        from services import rss

        class Boom:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(rss.httpx, "AsyncClient", lambda *a, **kw: Boom())
        result = await rss._fetch_single_feed("Test", "https://example.com/feed")
        assert result.articles == []
        assert "ConnectTimeout" in result.error
        assert result.source_name == "Test"
