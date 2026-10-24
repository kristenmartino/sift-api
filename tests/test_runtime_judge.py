"""Automatic significance notes stay blank with only headline/summary evidence.

The legacy judge flag and budget availability cannot re-enable unsupported
notes. Importance scores must still be written.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from services import context_generator as cg, judge as judge_service

URL1 = "https://example.com/keep"
URL2 = "https://example.com/judge-drop"

# Two clean, novel lines that both SURVIVE the deterministic gate (no cliché, not
# restatement) so they reach the judge.
LINE1 = "Grocery prices could climb because the cuts hit major vegetable farms."
LINE2 = "The settlement sets a precedent other universities will measure against."


class FakePool:
    def __init__(self):
        self.updates: list[tuple] = []

    async def fetchrow(self, _q, *_a):
        return {"metadata": {"ctx-0": [URL1, URL2]}}

    async def fetch(self, _q, *_a):
        return [
            {"source_url": URL1, "title": "Colorado River deal", "summary": "States agreed to water cuts."},
            {"source_url": URL2, "title": "OSU settlement", "summary": "Ohio State agreed to pay $100M."},
        ]

    async def execute(self, _q, *a):
        self.updates.append(a)  # (why_it_matters, importance_score, tone, genre, source_url)


def _results():
    payload = json.dumps([{"i": 1, "c": LINE1, "s": 3}, {"i": 2, "c": LINE2, "s": 4}])
    return [{
        "custom_id": "ctx-0",
        "result": {"type": "succeeded", "message": {"content": [{"type": "text", "text": payload}]}},
    }]


def _wire(monkeypatch, pool, *, budget_allowed=True):
    monkeypatch.setattr(cg, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        cg, "check_budget",
        AsyncMock(return_value=SimpleNamespace(allowed=budget_allowed, reason="ok")),
    )


@pytest.mark.asyncio
async def test_old_judge_flag_cannot_publish_a_note_without_evidence(monkeypatch):
    monkeypatch.setattr(settings, "why_it_matters_judge_enabled", True)
    pool = FakePool()
    _wire(monkeypatch, pool)
    # Even a positive legacy judge result cannot establish missing evidence.
    monkeypatch.setattr(judge_service, "judge_lines", AsyncMock(return_value=[
        {"id": URL1, "judged": True, "restates": False, "neutral_cliche_free": True},
        {"id": URL2, "judged": True, "restates": True, "neutral_cliche_free": True},
    ]))

    await cg.process_context_batch_results("batch-1", _results())

    writes = {u[4]: u for u in pool.updates}
    assert writes[URL1][0] is None          # withheld
    assert writes[URL2][0] is None           # judge-dropped -> NULL line
    assert writes[URL2][1] == 4              # ...but score still recorded


@pytest.mark.asyncio
async def test_flag_off_withholds_notes_and_skips_judge(monkeypatch):
    monkeypatch.setattr(settings, "why_it_matters_judge_enabled", False)
    pool = FakePool()
    _wire(monkeypatch, pool)
    judge = AsyncMock()
    monkeypatch.setattr(judge_service, "judge_lines", judge)

    await cg.process_context_batch_results("batch-1", _results())

    judge.assert_not_called()
    writes = {u[4]: u for u in pool.updates}
    assert writes[URL1][0] is None
    assert writes[URL2][0] is None


@pytest.mark.asyncio
async def test_budget_block_still_withholds_notes(monkeypatch):
    monkeypatch.setattr(settings, "why_it_matters_judge_enabled", True)
    pool = FakePool()
    _wire(monkeypatch, pool, budget_allowed=False)
    judge = AsyncMock()
    monkeypatch.setattr(judge_service, "judge_lines", judge)

    await cg.process_context_batch_results("batch-1", _results())

    judge.assert_not_called()              # budget blocked -> never judged
    writes = {u[4]: u for u in pool.updates}
    assert writes[URL1][0] is None        # withheld without additional source evidence
    assert writes[URL2][0] is None
