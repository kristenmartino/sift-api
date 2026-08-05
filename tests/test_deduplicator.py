"""Tests for services/deduplicator.

Covers the three drop rules and the one *observational* counter:
`dup_url_intra_kept`, which measures same-source_url articles surviving a
single batch without dropping them. See the comment in `deduplicate` for
why that case costs money.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from app.models import RSSArticle
from services.deduplicator import deduplicate


def _article(url: str, *, title: str = "T", content_hash: str | None = None) -> RSSArticle:
    return RSSArticle(
        title=title,
        source_url=url,
        source_name="Outlet",
        content_hash=content_hash,
    )


def _pool(rows: list[dict] | None = None) -> AsyncMock:
    """Pool whose single existing-rows query returns `rows`."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=rows or [])
    return pool


def _stats(caplog) -> dict:
    for record in caplog.records:
        msg = record.getMessage()
        if '"dedup_stats"' in msg:
            return json.loads(msg)
    raise AssertionError("no dedup_stats event was logged")


async def _run(articles, monkeypatch, *, existing=None):
    monkeypatch.setattr(
        "services.deduplicator.get_pool", AsyncMock(return_value=_pool(existing)),
    )
    return await deduplicate(articles)


@pytest.mark.asyncio
async def test_duplicate_url_in_one_batch_is_counted_but_not_dropped(monkeypatch, caplog):
    """Intra-batch dedup keys on content_hash, so the same url with edited body
    text survives twice — an outlet revising a story, or two section feeds of
    the same outlet carrying one piece. Both copies then get summarized and
    entity-linked at full price before collapsing to a single row at store
    time (ON CONFLICT (source_url)).

    Counting it is deliberately not the same as dropping it: dropping is a
    behavior change to ingest and there is no historical rate to justify it
    yet, because the collapse leaves no trace in the DB.
    """
    batch = [
        _article("https://e.com/a", title="Original", content_hash="h1"),
        _article("https://e.com/a", title="Revised", content_hash="h2"),
    ]

    with caplog.at_level(logging.INFO, logger="sift-api.deduplicator"):
        out = await _run(batch, monkeypatch)

    # Not dropped — both survive and both will be paid for downstream.
    assert len(out) == 2
    stats = _stats(caplog)
    assert stats["dup_url_intra_kept"] == 1
    assert stats["new"] == 2


@pytest.mark.asyncio
async def test_distinct_urls_report_no_intra_batch_duplicates(monkeypatch, caplog):
    """The counter must stay at 0 on a clean batch, or it is just noise."""
    batch = [
        _article("https://e.com/a", content_hash="h1"),
        _article("https://e.com/b", content_hash="h2"),
    ]

    with caplog.at_level(logging.INFO, logger="sift-api.deduplicator"):
        out = await _run(batch, monkeypatch)

    assert len(out) == 2
    assert _stats(caplog)["dup_url_intra_kept"] == 0


@pytest.mark.asyncio
async def test_url_already_in_db_is_dropped_before_the_counter(monkeypatch, caplog):
    """A url the DB already has is dropped outright, so it must not also be
    reported as an intra-batch duplicate — the two are different problems."""
    batch = [_article("https://e.com/a", content_hash="h1")]

    with caplog.at_level(logging.INFO, logger="sift-api.deduplicator"):
        out = await _run(
            batch, monkeypatch,
            existing=[{"source_url": "https://e.com/a", "content_hash": None}],
        )

    assert out == []
    stats = _stats(caplog)
    assert stats["dropped_url"] == 1
    assert stats["dup_url_intra_kept"] == 0


@pytest.mark.asyncio
async def test_same_content_hash_still_drops_the_second_copy(monkeypatch, caplog):
    """Pins the pre-existing rule the counter sits next to: identical body text
    across two urls (AP syndication) is dropped, not counted."""
    batch = [
        _article("https://e.com/a", content_hash="same"),
        _article("https://e.com/b", content_hash="same"),
    ]

    with caplog.at_level(logging.INFO, logger="sift-api.deduplicator"):
        out = await _run(batch, monkeypatch)

    assert [a.source_url for a in out] == ["https://e.com/a"]
    stats = _stats(caplog)
    assert stats["dropped_hash_intra"] == 1
    assert stats["dup_url_intra_kept"] == 0
