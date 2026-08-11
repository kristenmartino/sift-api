"""Alignment enforcement in the three Batch-API enrichment services.

Same failure class as the summarizer bug (see tests/test_summarizer.py): the
model's own returned index was the only thing tying a result to an article, and
only its RANGE was checked. A repeated, skipped, or shifted index silently
attached why_it_matters / context_primer / entities to the WRONG article.

Two paths per service, with different answers to "what now?":

  live path      — re-ask the batch (services.index_alignment.with_alignment_retry),
                   then degrade to something that cannot misalign.
  Batch API path — results arrive asynchronously through the poller, so there is
                   no request to repeat: the sub-batch is skipped whole and its
                   columns stay NULL, which the backfill scripts can repair.

Each "writes nothing" test is paired with a happy-path control, so a handler
that silently stopped writing altogether could not pass both.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from services import context_generator as cg
from services import entity_extractor as ee
from services import primer_generator as pg

URL1 = "https://example.com/one"
URL2 = "https://example.com/two"

# Survives the deterministic why_it_matters gate (no cliché, not a restatement).
GOOD_LINE = "Grocery prices could climb because the cuts hit major vegetable farms."
GOOD_BACKGROUND = (
    "Venture firms raise money from investors and deploy it into startups over a fund's ten-year life."
)


def _articles() -> list[dict]:
    return [
        {"source_url": URL1, "title": "Colorado River deal", "summary": "States agreed to water cuts.",
         "source_name": "Outlet1"},
        {"source_url": URL2, "title": "OSU settlement", "summary": "Ohio State agreed to pay $100M.",
         "source_name": "Outlet2"},
    ]


def _mock_client(*texts: str) -> AsyncMock:
    """Replay one response per call. Mirrors tests/test_story_clusterer.py."""
    client = AsyncMock()
    client.messages.create = AsyncMock(
        side_effect=[
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
            )
            for text in texts
        ]
    )
    return client


def _batch_results(custom_id: str, payload: list[dict]) -> list[dict]:
    """One JSONL result line, shaped like services.batch_client hands it over."""
    return [{
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        },
    }]


class FakePool:
    """Metadata in, UPDATEs recorded. Mirrors tests/test_runtime_judge.py."""

    def __init__(self, custom_id: str):
        self.metadata = {custom_id: [URL1, URL2]}
        self.updates: list[tuple] = []

    async def fetchrow(self, _q, *_a):
        return {"metadata": self.metadata}

    async def fetch(self, _q, *_a):
        return [
            {"source_url": URL1, "title": "Colorado River deal", "summary": "States agreed to water cuts."},
            {"source_url": URL2, "title": "OSU settlement", "summary": "Ohio State agreed to pay $100M."},
        ]

    async def execute(self, _q, *a):
        self.updates.append(a)


def _misaligned_events(caplog, event: str) -> list[dict]:
    return [
        json.loads(r.message)
        for r in caplog.records
        if r.message.startswith("{") and f'"{event}"' in r.message
    ]


class TestContextLivePath:
    @pytest.mark.asyncio
    async def test_duplicate_index_is_retried_and_the_retry_is_used(self):
        client = _mock_client(
            json.dumps([{"i": 1, "c": GOOD_LINE, "s": 3}, {"i": 1, "c": GOOD_LINE, "s": 4}]),
            json.dumps([{"i": 1, "c": GOOD_LINE, "s": 3}, {"i": 2, "c": GOOD_LINE, "s": 4}]),
        )
        out = await cg.generate_context(_articles(), client=client)
        assert client.messages.create.await_count == 2
        assert out[URL1]["score"] == 3
        assert out[URL2]["score"] == 4

    @pytest.mark.asyncio
    async def test_persistent_misalignment_writes_nothing_for_the_batch(self):
        # A response for article 2 only: which article it describes is unknowable.
        shifted = json.dumps([{"i": 2, "c": GOOD_LINE, "s": 5}])
        client = _mock_client(shifted, shifted)
        out = await cg.generate_context(_articles(), client=client)
        assert out == {}


class TestContextBatchPath:
    @pytest.mark.asyncio
    async def test_aligned_sub_batch_is_written(self, monkeypatch):
        monkeypatch.setattr(settings, "why_it_matters_judge_enabled", False)
        pool = FakePool("ctx-0")
        monkeypatch.setattr(cg, "get_pool", AsyncMock(return_value=pool))

        await cg.process_context_batch_results("batch-1", _batch_results(
            "ctx-0", [{"i": 1, "c": GOOD_LINE, "s": 3, "t": "grim"}, {"i": 2, "c": GOOD_LINE, "s": 4}],
        ))

        assert {u[3] for u in pool.updates} == {URL1, URL2}
        # Params are (line, score, tone, url): tone reaches the write path,
        # and a missing "t" lands on neutral, never grim.
        tones = {u[3]: u[2] for u in pool.updates}
        assert tones == {URL1: "grim", URL2: "neutral"}

    @pytest.mark.asyncio
    async def test_misaligned_sub_batch_is_skipped_whole(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "why_it_matters_judge_enabled", False)
        pool = FakePool("ctx-0")
        monkeypatch.setattr(cg, "get_pool", AsyncMock(return_value=pool))

        with caplog.at_level(logging.INFO, logger="sift-api.context_generator"):
            # Index 1 twice: one of these lines belongs to article 2.
            await cg.process_context_batch_results("batch-1", _batch_results(
                "ctx-0", [{"i": 1, "c": GOOD_LINE, "s": 3}, {"i": 1, "c": GOOD_LINE, "s": 4}],
            ))

        assert pool.updates == []
        events = _misaligned_events(caplog, "batch_context_misaligned")
        assert events and events[0]["source_urls"] == [URL1, URL2]

    @pytest.mark.asyncio
    async def test_sub_batch_without_a_url_manifest_is_skipped(self, monkeypatch):
        # Previously every entry just failed the range check against len([]) == 0
        # and the sub-batch vanished with nothing logged.
        monkeypatch.setattr(settings, "why_it_matters_judge_enabled", False)
        pool = FakePool("ctx-0")
        pool.metadata = {}
        monkeypatch.setattr(cg, "get_pool", AsyncMock(return_value=pool))

        await cg.process_context_batch_results("batch-1", _batch_results(
            "ctx-0", [{"i": 1, "c": GOOD_LINE, "s": 3}],
        ))

        assert pool.updates == []


class TestPrimerLivePath:
    @pytest.mark.asyncio
    async def test_duplicate_index_is_retried_and_the_retry_is_used(self):
        client = _mock_client(
            json.dumps([{"i": 1, "b": GOOD_BACKGROUND, "t": []}, {"i": 1, "b": GOOD_BACKGROUND, "t": []}]),
            json.dumps([{"i": 1, "b": GOOD_BACKGROUND, "t": []}, {"i": 2, "b": GOOD_BACKGROUND, "t": []}]),
        )
        out = await pg.generate_primers(_articles(), client=client)
        assert client.messages.create.await_count == 2
        assert set(out) == {URL1, URL2}

    @pytest.mark.asyncio
    async def test_persistent_misalignment_writes_nothing_for_the_batch(self):
        shifted = json.dumps([{"i": 2, "b": GOOD_BACKGROUND, "t": []}])
        client = _mock_client(shifted, shifted)
        assert await pg.generate_primers(_articles(), client=client) == {}


class TestPrimerBatchPath:
    @pytest.mark.asyncio
    async def test_aligned_sub_batch_is_written(self, monkeypatch):
        pool = FakePool("primer-0")
        monkeypatch.setattr(pg, "get_pool", AsyncMock(return_value=pool))

        await pg.process_primer_batch_results("batch-1", _batch_results(
            "primer-0",
            [{"i": 1, "b": GOOD_BACKGROUND, "t": []}, {"i": 2, "b": GOOD_BACKGROUND, "t": []}],
        ))

        assert {u[1] for u in pool.updates} == {URL1, URL2}

    @pytest.mark.asyncio
    async def test_misaligned_sub_batch_is_skipped_whole(self, monkeypatch, caplog):
        pool = FakePool("primer-0")
        monkeypatch.setattr(pg, "get_pool", AsyncMock(return_value=pool))

        with caplog.at_level(logging.INFO, logger="sift-api.primer_generator"):
            await pg.process_primer_batch_results("batch-1", _batch_results(
                "primer-0",
                [{"i": 1, "b": GOOD_BACKGROUND, "t": []}, {"i": 1, "b": GOOD_BACKGROUND, "t": []}],
            ))

        assert pool.updates == []
        events = _misaligned_events(caplog, "batch_primer_misaligned")
        assert events and events[0]["source_urls"] == [URL1, URL2]


class TestEntityLivePath:
    @pytest.mark.asyncio
    async def test_duplicate_index_is_retried_and_the_retry_is_used(self):
        client = _mock_client(
            json.dumps([{"i": 1, "p": ["Alice"]}, {"i": 1, "p": ["Bob"]}]),
            json.dumps([{"i": 1, "p": ["Alice"]}, {"i": 2, "p": ["Bob"]}]),
        )
        out = await ee.extract_entities(_articles(), client=client)
        assert client.messages.create.await_count == 2
        assert out[URL1]["people"] == ["Alice"]
        assert out[URL2]["people"] == ["Bob"]

    @pytest.mark.asyncio
    async def test_persistent_misalignment_degrades_to_empty_not_borrowed_entities(self):
        # Entities on the wrong article would corrupt story clustering, which
        # uses them to decide what covers the same event.
        shifted = json.dumps([{"i": 2, "p": ["Bob"], "o": ["Acme"]}])
        client = _mock_client(shifted, shifted)
        out = await ee.extract_entities(_articles(), client=client)
        assert out[URL1] == ee._empty_entities()
        assert out[URL2] == ee._empty_entities()


class TestEntityBatchPath:
    @pytest.mark.asyncio
    async def test_aligned_sub_batch_is_written(self, monkeypatch):
        pool = FakePool("ent-0")
        monkeypatch.setattr(ee, "get_pool", AsyncMock(return_value=pool))

        await ee.process_entity_batch_results("batch-1", _batch_results(
            "ent-0", [{"i": 1, "p": ["Alice"]}, {"i": 2, "p": ["Bob"]}],
        ))

        assert {u[1] for u in pool.updates} == {URL1, URL2}

    @pytest.mark.asyncio
    async def test_misaligned_sub_batch_is_skipped_whole(self, monkeypatch, caplog):
        pool = FakePool("ent-0")
        monkeypatch.setattr(ee, "get_pool", AsyncMock(return_value=pool))

        with caplog.at_level(logging.INFO, logger="sift-api.entity_extractor"):
            # Three entries for a two-article sub-batch.
            await ee.process_entity_batch_results("batch-1", _batch_results(
                "ent-0", [{"i": 1, "p": ["Alice"]}, {"i": 2, "p": ["Bob"]}, {"i": 3, "p": ["Carol"]}],
            ))

        assert pool.updates == []
        events = _misaligned_events(caplog, "batch_entity_misaligned")
        assert events and events[0]["source_urls"] == [URL1, URL2]
