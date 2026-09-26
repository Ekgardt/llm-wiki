"""Reclaim names what it could not do, and doctor names this week's dead tasks (audit 2026-09-26 B-23).

docs/research/2026-09-26-a-failure-in-the-night-is-named.md
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import doctor
import reclaim_runtime_state

HEALTHY = {
    "backlog": {"drained": {}, "remaining": [], "failed": []},
    "transactions": {"pruned": 0, "failed": 0},
    "history": {"attempts": 0, "transactions": 0, "failed": 0},
    "temporaries": {"removed": 0, "bytes": 0},
    "staged_writes": {"removed": 0, "bytes": 0},
    "empty_shards": 0,
    "snapshot": {"status": "ok", "commit": "abc"},
    "co_activation": {"co_activation_pages": 3},
}


def test_a_failed_prune_is_in_the_report_and_fails_the_step() -> None:
    result = {**HEALTHY, "history": {"attempts": 0, "transactions": 0, "failed": 1, "reason": "database is locked"}}

    assert ("FAILED: history: database is locked" in reclaim_runtime_state._report(result), reclaim_runtime_state._failures(result)) == (
        True,
        ["history: database is locked"],
    )


def test_a_healthy_pass_names_no_failure() -> None:
    assert (reclaim_runtime_state._failures(HEALTHY), "FAILED" in reclaim_runtime_state._report(HEALTHY)) == ([], False)


def _rows(*updated: datetime) -> list[sqlite3.Row]:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE tasks(state TEXT, updated_at TEXT)")
    database.executemany("INSERT INTO tasks VALUES ('dead', ?)", [(moment.isoformat(),) for moment in updated])
    return database.execute("SELECT * FROM tasks").fetchall()


def test_only_this_weeks_dead_tasks_count() -> None:
    now = datetime.now(timezone.utc)

    assert doctor._recent_dead_count(_rows(now - timedelta(days=1), now - timedelta(days=30)), now) == 1
