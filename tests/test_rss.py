from __future__ import annotations

import re
from pathlib import Path

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
