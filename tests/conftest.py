from __future__ import annotations

import pytest


@pytest.fixture
def sample_rss_xml():
    """A minimal RSS 2.0 feed for testing."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
    <title>Test Feed</title>
    <link>https://example.com</link>
    <item>
        <title>Test Article One</title>
        <link>https://example.com/article-1</link>
        <description>This is the first test article about technology.</description>
        <pubDate>Fri, 28 Mar 2026 12:00:00 GMT</pubDate>
        <media:content url="https://example.com/image1.jpg" type="image/jpeg" />
    </item>
    <item>
        <title>Test Article Two</title>
        <link>https://example.com/article-2</link>
        <description>This is the second test article about science.</description>
        <pubDate>Fri, 28 Mar 2026 11:00:00 GMT</pubDate>
    </item>
    <item>
        <title>Test Article Three</title>
        <link>https://example.com/article-3</link>
        <description>Third article with an enclosure image.</description>
        <pubDate>Fri, 28 Mar 2026 10:00:00 GMT</pubDate>
        <enclosure url="https://example.com/image3.jpg" type="image/png" />
    </item>
</channel>
</rss>"""


@pytest.fixture
def sample_atom_xml():
    """A minimal Atom feed for testing."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
    <title>Atom Test Feed</title>
    <link href="https://example.com" />
    <entry>
        <title>Atom Article</title>
        <link href="https://example.com/atom-1" />
        <summary>An atom feed article.</summary>
        <updated>2026-03-28T09:00:00Z</updated>
    </entry>
</feed>"""
