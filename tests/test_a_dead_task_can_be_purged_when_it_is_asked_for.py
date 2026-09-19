"""Attempts-exhausted work leaves the adopted queue when an operator asks for it.

`dead` is terminal, so `cancel` refuses it, and `purge(include_dead=True)` was
refused outright on the adopted queue: a task that exhausted its attempts stayed
for good, and a retained queue task blocks deleting `run/` by contract. A row
demoted to `dead / payload_hash_mismatch` still stays, because it cannot be
exported; `quarantine-corrupt` is its route.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memory_queue

from tests.adopted_vault import adopt, tamper_payload, task_state

_LONG_AGO = "2026-01-01T00:00:00.000000+00:00"


def _kill_and_age(state_root: Path, task_ids: tuple[str, ...]) -> None:
    """Leave the tasks dead and old, as exhausted attempts do past retention."""
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.executemany(
            "UPDATE tasks SET state='dead',error_code='boom',updated_at=? WHERE id=?",
            [(_LONG_AGO, task_id) for task_id in task_ids],
        )


def _purge(queue, tmp_path: Path, *, include_dead: bool, name: str):
    return queue.purge(
        terminal_before=datetime.now(timezone.utc) - timedelta(days=1),
        export_path=tmp_path / name,
        include_dead=include_dead,
    )


def test_a_dead_task_is_retained_until_it_is_asked_for(tmp_path: Path) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    dead = queue.enqueue("compile", 1, {"daily": "one"})
    _kill_and_age(state_root, (dead,))

    kept = _purge(queue, tmp_path, include_dead=False, name="kept")
    taken = _purge(queue, tmp_path, include_dead=True, name="taken")

    assert (kept.task_ids, taken.task_ids) == ((), (dead,))


def test_a_corrupt_dead_row_stays_for_the_corruption_route(tmp_path: Path) -> None:
    root, state_root = adopt(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    healthy = queue.enqueue("compile", 1, {"daily": "one"})
    corrupt = queue.enqueue("compile", 1, {"daily": "two"})
    queue.cancel(corrupt)
    _kill_and_age(state_root, (healthy,))
    _kill_and_age(state_root, (corrupt,))
    tamper_payload(state_root, corrupt)

    receipt = _purge(queue, tmp_path, include_dead=True, name="export")

    assert (receipt.task_ids, task_state(state_root, corrupt)) == (
        (healthy,),
        ("dead", "payload_hash_mismatch"),
    )
