"""Tests for opinion-genre detection (ranking v2 stage 4)."""
from __future__ import annotations

from services.genre import detect_opinion


class TestDetectOpinion:
    def test_path_segment_anywhere(self):
        # NY Post buries /opinion/ after the date; The Hill leads with it.
        assert detect_opinion("https://nypost.com/2026/07/31/opinion/some-piece/", None)
        assert detect_opinion("https://thehill.com/opinion/campaign/123-x/", None)
        assert detect_opinion("https://www.theguardian.com/commentisfree/2026/aug/x", None)
        assert detect_opinion("https://www.washingtonexaminer.com/opinion/editorials/1/x", None)

    def test_segment_must_be_whole(self):
        # "opinionated" in a slug is not an opinion section.
        assert not detect_opinion("https://example.com/news/opinionated-voters-shift/", None)
        # Nor a domain containing the word.
        assert not detect_opinion("https://opinionjournal.example.com/news/x", None)

    def test_title_prefixes(self):
        assert detect_opinion(None, "Opinion: Europe's summer of heat")
        assert detect_opinion(None, "Editorial | The case for reform")
        assert not detect_opinion(None, "Analysis: what the ruling means")  # reported-adjacent
        assert not detect_opinion(None, "Opinions differ on new tax rule")  # no delimiter

    def test_plain_news_is_not_flagged(self):
        assert not detect_opinion(
            "https://www.cbsnews.com/news/earthquake-colombia-updates/",
            "7.4 magnitude earthquake rocks Colombia",
        )

    def test_none_inputs(self):
        assert not detect_opinion(None, None)
