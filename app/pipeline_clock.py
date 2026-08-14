"""In-memory mirror of pipeline_state.last_refreshed_at.

/health was the second-largest source of Neon wakeups. It ran SELECT 1 plus
SELECT MAX(last_refreshed_at) FROM pipeline_state, on a handler the GitHub
heartbeat calls every 30 minutes (.github/workflows/pipeline-heartbeat.yml).
Two queries, 30 minutes apart, are on their own enough to keep a compute with
a 5-minute scale-to-zero window from ever suspending.

The value is knowable without asking. This process is the ONLY writer of
pipeline_state.last_refreshed_at (workflows/pipeline_workflow.py, store_node),
so it can seed once at startup — inside the awake window _apply_migrations
already creates — and update itself thereafter.

SINGLE-REPLICA ASSUMPTION. This is already load-bearing elsewhere: two
replicas would each run their own _scheduled_refresh and their own batch
poller (app/main.py). If sift-api is ever scaled horizontally, this module and
both of those have to be revisited together.
"""
from __future__ import annotations

from datetime import datetime, timezone

_last_pipeline_run: datetime | None = None
_db_ok: bool = False


def note_pipeline_run(when: datetime | None = None) -> None:
    """Record that a pipeline run just wrote pipeline_state.

    Called from store_node rather than from the caller, so that both entry
    points — the scheduled loop and POST /pipeline/refresh, which is the
    heartbeat's self-heal path — advance the clock.
    """
    global _last_pipeline_run, _db_ok
    _last_pipeline_run = when or datetime.now(timezone.utc)
    _db_ok = True


def seed(last_run: datetime | None, db_ok: bool) -> None:
    """Startup seed, from the one authoritative read we still pay for."""
    global _last_pipeline_run, _db_ok
    _last_pipeline_run = last_run
    _db_ok = db_ok


def note_db_error() -> None:
    global _db_ok
    _db_ok = False


def last_pipeline_run() -> datetime | None:
    return _last_pipeline_run


def db_ok() -> bool:
    return _db_ok


def _reset_for_tests() -> None:
    """Clear module state between tests. Wired into an autouse conftest fixture."""
    global _last_pipeline_run, _db_ok
    _last_pipeline_run = None
    _db_ok = False
