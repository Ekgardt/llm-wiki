"""A redriven capture meets no leftover decision, and its family does not stop the purge.

Audit 2026-09-26 A-12, docs/research/2026-09-26-a-redriven-capture-meets-no-leftovers.md.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import flush_memory
import operational_ownership
import pytest

from tests.test_capture_terminal import (  # noqa: F401
    _crash_before_ledger,
    _FakeNoContentProvider,
    _NoContentProcessor,
    _own_session_vault,
    _ready_intent_binding,
)
from tests.test_queue_v3_capture_links import _coordinator, _queue


def _died_between_decision_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, operational_ownership.OwnershipRegistry(tmp_path), "status only")
    processor = _NoContentProcessor(queue, coordinator, _FakeNoContentProvider())
    with monkeypatch.context() as patch, pytest.raises(RuntimeError, match="ledger crash"):
        patch.setattr(queue, "publish_semantic_decision", _crash_before_ledger)
        flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)
    with sqlite3.connect(queue.db_path) as database:
        database.execute("UPDATE tasks SET state='dead', lease_owner=NULL, lease_expires_at=NULL WHERE id=?", (binding.task_id,))
    return queue, coordinator, binding, processor


def _age_everything(queue) -> None:
    with sqlite3.connect(queue.db_path) as database:
        database.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00'")


def test_the_redrive_completes_the_capture_its_parent_left(tmp_path, monkeypatch) -> None:
    queue, coordinator, binding, processor = _died_between_decision_and_ledger(tmp_path, monkeypatch)
    queue.redrive(binding.task_id)

    relative = flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)

    assert relative == f"run/queue-results/capture-{binding.intent_id}.json"


def test_the_purge_keeps_the_redrive_family_and_does_not_stop(tmp_path, monkeypatch) -> None:
    queue, coordinator, binding, processor = _died_between_decision_and_ledger(tmp_path, monkeypatch)
    child = queue.redrive(binding.task_id)
    flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)
    _age_everything(queue)

    receipt = queue.purge(
        terminal_before=datetime.now(timezone.utc), export_path=tmp_path / "export", include_dead=True
    )

    assert set(receipt.retained) == {binding.task_id, child}
