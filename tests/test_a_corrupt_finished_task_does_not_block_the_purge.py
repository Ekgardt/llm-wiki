"""A corrupt finished task leaves the purge selection instead of refusing the plan.

The ordinary purge selects every old succeeded or cancelled row and used to
raise `payload_hash_mismatch` for the whole plan when one of them was corrupt.
Nothing else can move a finished row, so every later purge failed as well.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memory_queue

from tests.adopted_vault import adopt, tamper_payload, task_state

_LONG_AGO = "2026-01-01T00:00:00.000000+00:00"


def _age(state_root: Path, task_ids: tuple[str, ...]) -> None:
    """Make the tasks as old as a finished task is after its retention passed."""
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.executemany(
            "UPDATE tasks SET updated_at=? WHERE id=?",
            [(_LONG_AGO, task_id) for task_id in task_ids],
        )


def test_the_purge_takes_the_healthy_task_and_demotes_the_corrupt_one(
    tmp_path: Path,
) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    healthy = queue.enqueue("compile", 1, {"daily": "one"})
    corrupt = queue.enqueue("compile", 1, {"daily": "two"})
    queue.cancel(healthy)
    queue.cancel(corrupt)
    _age(state_root, (healthy, corrupt))
    tamper_payload(state_root, corrupt)

    receipt = queue.purge(
        terminal_before=datetime.now(timezone.utc) - timedelta(days=1),
        export_path=tmp_path / "export",
    )

    assert receipt.task_ids == (healthy,)
    assert task_state(state_root, corrupt) == ("dead", "payload_hash_mismatch")
