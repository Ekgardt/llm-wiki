"""The capture worker retries a capture whose previous worker died.

`claim_capture` selects `state='ready'` only, so an expired lease is invisible
to it. On the v2 runtime `claim` swept expired leases itself; the adopted V3
queue dropped that, and doctor's recovery walks the retired file queue rather
than the SQLite one. Nothing else called `recover_expired_leases` in the
product, so a worker killed mid-capture stranded its task forever.

Every earlier test swept by hand before claiming, which is why none of them
saw it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import flush_memory  # noqa: E402
import operational_ownership  # noqa: E402

from tests.test_queue_v3_capture_links import (  # noqa: E402
    _capture_binding,
    _coordinator,
    _queue,
)


def _abandon(queue, task_id: str) -> None:
    """Leave the lease behind exactly as a killed worker would."""
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )


def _state(queue, task_id: str) -> str:
    with sqlite3.connect(queue.db_path) as database:
        row = database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    return str(row[0])


def test_the_capture_worker_retries_a_capture_its_predecessor_abandoned(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="a" * 64,
        intent_sha256="b" * 64,
    )
    first = queue.claim_capture("capture-worker")
    assert first is not None
    _abandon(queue, first.id)
    assert _state(queue, binding.task_id) == "leased"

    claimed: list[str] = []

    def _record(lease, *_rest, **_kwargs):
        # `process_missing` is bound to the queue and coordinator already, so
        # the first argument left here is the lease the worker claimed.
        claimed.append(lease.id)
        return None

    flush_memory.run_capture_worker_once(
        queue, coordinator, process_missing=_record
    )
    assert claimed == [binding.task_id]
