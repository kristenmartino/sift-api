from types import SimpleNamespace

import pytest

from scripts import audit_source_copy as audit


@pytest.mark.asyncio
@pytest.mark.parametrize(('override_id', 'override_verdict', 'fails'), [
    (None, None, False),
    ('actual_bengals', 'retry', True),
    ('political_bridge', 'pass', True),
    ('passing_reference', 'error', True),
    ('different_game', 'unavailable', False),
])
async def test_event_controls_detect_false_acceptance_false_rejection_and_inconclusive_review(
        monkeypatch, override_id, override_verdict, fails):
    async def review(rows):
        result = {r['id']: {'v': 'pass' if r['expected_same_event'] else 'retry'} for r in rows}
        if override_id:
            result[override_id] = {'v': override_verdict}
        return result
    monkeypatch.setattr(audit, 'assess_copy', review)
    assert await audit._run_audit(SimpleNamespace(event_regressions=True)) is fails
