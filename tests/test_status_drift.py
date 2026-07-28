"""Drift guards for hand-maintained counts in STATUS.md.

Why this exists: STATUS.md claimed the pipeline "ingests ~135 sources" when
`len(FEEDS)` was 58. Nobody noticed for weeks, and the wrong number was then
copied into sift/docs/OPERATING_CONTEXT.md and sift/docs/LAUNCH_DECISION_MEMO.md
and two PR descriptions as though it had been verified.

#104 adds the equivalent guard for README.md. This file deliberately covers
STATUS.md only, in its own module, so the two changes don't collide in
tests/test_rss.py — and because STATUS.md is the file people actually read
first, so a wrong number there travels furthest.

Keep these narrow: assert numbers that are derivable from code. Prose is not
testable and shouldn't be asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.rss import FEEDS

STATUS = Path(__file__).resolve().parent.parent / "STATUS.md"

# Matches: ingests **58 RSS feeds**
# Anchored on "ingests" and "RSS feeds" so it cannot accidentally match another
# number in the same sentence (the line currently also contains "135" inside a
# note recording the correction).
FEED_COUNT_RE = re.compile(r"ingests\s+\*\*(\d+)\s+RSS feeds\*\*", re.IGNORECASE)


@pytest.fixture(scope="module")
def status_text() -> str:
    assert STATUS.exists(), f"STATUS.md not found at {STATUS}"
    return STATUS.read_text(encoding="utf-8")


class TestStatusFeedCount:
    def test_status_states_a_feed_count(self, status_text: str):
        """The claim must be present and machine-readable, or this guard is inert."""
        assert FEED_COUNT_RE.search(status_text), (
            "STATUS.md no longer contains a parseable 'ingests **N RSS feeds**' "
            "claim. If the wording changed, update FEED_COUNT_RE here — do not "
            "delete this test. An unparseable claim is how the last drift hid."
        )

    def test_status_feed_count_matches_code(self, status_text: str):
        """STATUS.md's feed count must equal len(FEEDS)."""
        match = FEED_COUNT_RE.search(status_text)
        assert match is not None
        claimed = int(match.group(1))
        assert claimed == len(FEEDS), (
            f"STATUS.md claims {claimed} RSS feeds but services.rss.FEEDS has "
            f"{len(FEEDS)}. Update STATUS.md (and check whether the stale number "
            f"has been copied into sift/docs/OPERATING_CONTEXT.md, which is what "
            f"happened last time)."
        )

    def test_feeds_is_flat_name_url_pairs(self):
        """Guards the assumption the count rests on.

        The 135-vs-58 error was plausible partly because nobody had checked
        whether FEEDS was flat or nested — a dict of outlet -> [urls] could
        legitimately yield more URLs than entries. It is flat, and this fails
        if that ever changes, so 'feeds' and 'URLs' stay the same number.
        """
        assert isinstance(FEEDS, list)
        for entry in FEEDS:
            assert isinstance(entry, tuple) and len(entry) == 2, (
                f"FEEDS entry is no longer a (name, url) pair: {entry!r}. "
                "If FEEDS becomes nested, STATUS.md must distinguish feeds "
                "from source URLs."
            )
            name, url = entry
            assert isinstance(name, str) and name
            assert isinstance(url, str) and url.startswith("http")

        urls = [url for _, url in FEEDS]
        assert len(set(urls)) == len(urls), "duplicate feed URLs in FEEDS"
