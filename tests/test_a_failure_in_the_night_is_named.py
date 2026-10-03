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
    database.execute("CREATE TABLE tasks(id TEXT, state TEXT, updated_at TEXT)")
    database.executemany(
        "INSERT INTO tasks VALUES (?, 'dead', ?)",
        [(f"t{index}", moment.isoformat()) for index, moment in enumerate(updated)],
    )
    return database.execute("SELECT * FROM tasks").fetchall()


def test_every_dead_task_counts_and_the_oldest_age_is_shown() -> None:
    """An old dead task is still unresolved (audit 2026-09-27 B-13)."""
    now = datetime.now(timezone.utc)

    assert doctor._dead_backlog(_rows(now - timedelta(days=1), now - timedelta(days=30)), now) == (2, 30)


def _lineage(*children: str) -> list[sqlite3.Row]:
    """One dead parent and redrives of it in the given states."""
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("CREATE TABLE tasks(id TEXT, state TEXT, updated_at TEXT, redrive_of TEXT)")
    moment = datetime(2026, 9, 8, tzinfo=timezone.utc).isoformat()
    database.execute("INSERT INTO tasks VALUES ('parent', 'dead', ?, NULL)", (moment,))
    database.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?, 'parent')",
        [(f"child{index}", state, moment) for index, state in enumerate(children)],
    )
    return database.execute("SELECT * FROM tasks").fetchall()


def test_a_dead_task_a_redrive_answered_is_not_counted_again() -> None:
    """25 live dead captures, every one redriven, were named unresolved (2026-09-28)."""
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)

    assert [
        doctor._dead_backlog(_lineage(*children), now)[0]
        for children in ((), ("cancelled",), ("cancelled", "succeeded"), ("ready",), ("dead",))
    ] == [1, 1, 0, 0, 1]


def test_the_queue_message_names_the_dead_tasks_it_counts() -> None:
    assert doctor._queue_message("degraded", {"dead_unresolved": 16, "oldest_dead_days": 19}) == (
        "16 dead queue task(s) no redrive has answered, the oldest 19 day(s) old."
    )
