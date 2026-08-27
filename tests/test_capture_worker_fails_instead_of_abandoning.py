"""A capture the worker cannot finish becomes a named retry, not a stuck lease.

Measured live on 2026-08-27: the provider exceeded its ceiling, the worker
raised, the CLI boundary swallowed it with exit 0, and the task sat `leased`
until TTL expiry — three silent attempts with no recorded reason. The queue
already has the word for this (`processor_failed`, retry later); the capture
worker just never said it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

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


def _row(queue, task_id: str) -> tuple[str, object]:
    with sqlite3.connect(queue.db_path) as database:
        row = database.execute(
            "SELECT state, error_code FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    return str(row[0]), row[1]


def _boom(*_args, **_kwargs):
    raise RuntimeError("capture provider did not return a provider result")


def test_a_processing_failure_fails_the_task_with_a_named_retry(
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
    with pytest.raises(RuntimeError, match="provider result"):
        flush_memory.run_capture_worker_once(
            queue, coordinator, process_missing=_boom
        )
    state, error_code = _row(queue, binding.task_id)
    assert state == "ready", "the failure must settle the claim, not abandon it"
    assert error_code == "processor_failed"
