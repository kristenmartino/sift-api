"""Tests for opinion-genre detection (ranking v2 stage 4)."""
from __future__ import annotations

from services.genre import detect_opinion, detect_roundup


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



class TestDetectRoundup:
    def test_bloomberg_episode_titles(self):
        assert detect_roundup("Trump Hardens Stance on Iran, Nvidia Taps Wall Street for $500B | The Opening Trade 8/11/2026")
        assert detect_roundup("Bloomberg This Weekend 08/09/2026")

    def test_cbs_show_suffixes(self):
        # The original doom-feed session's pinned #1 was one of these.
        assert detect_roundup("D4vd Charged with Murder | Case by Case")
        assert detect_roundup("Exonerees, crime survivors come together for healing | 60 Minutes")

    def test_named_briefs_and_npr_format(self):
        assert detect_roundup("Morning news brief")
        assert detect_roundup("The Evening: An Earthquake Shakes Colombia")
        assert detect_roundup("3 dead in mass shooting in Seattle. And, U.S. and Iran pause fighting")

    def test_plain_headlines_are_not_roundups(self):
        assert not detect_roundup("7.4 magnitude earthquake rocks Colombia, over 100 dead")
        # A pipe alone is not a show marker.
        assert not detect_roundup("Jones v. Smith | what the ruling means")
        # "And," mid-sentence without the period boundary is prose.
        assert not detect_roundup("Senators and, surprisingly, governors agree")
        assert not detect_roundup(None)
