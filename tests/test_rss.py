from __future__ import annotations

from services.rss import stable_hash, _base36, parse_feed, FEEDS


class TestStableHash:
    """Test the djb2 hash port from JS."""

    def test_known_values(self):
        # These should be deterministic and stable
        h1 = stable_hash("hello")
        h2 = stable_hash("hello")
        assert h1 == h2

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
        # Flat list of (name, url) tuples — 128 feeds after adding politics/sports/entertainment
        assert len(FEEDS) >= 100

    def test_feeds_are_tuples(self):
        for feed in FEEDS:
            assert isinstance(feed, tuple), f"Feed {feed} is not a tuple"
            assert len(feed) == 2, "Feed tuple should have 2 elements"
            name, url = feed
            assert isinstance(name, str)
            assert url.startswith("http")
