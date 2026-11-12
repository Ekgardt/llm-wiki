"""A redrive reuses the semantic decision its dead parent already indexed.

Audit 2026-09-27 B-14. A capture task that died after its decision was sealed and
indexed left a `semantic_decisions` row for the intent; its redriven child met that
row with its own task id and link and failed its one second chance with
`semantic_decision_conflict`.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import flush_memory
import operational_ownership
import pytest
import queue_core  # noqa: E402

from tests.capture_terminal_helpers import (  # noqa: F401
    _assert_major_daily_file_once,
    _FakeNoContentProvider,
    _NoContentProcessor,
    _own_session_vault,
    _ready_intent_binding,
    _TimedCaptureProcessor,
)
from tests.queue_v3_capture_links_helpers import _coordinator, _queue


def _crash_after_decision(*_args, **_kwargs):
    raise RuntimeError("injected crash after the decision was indexed")


def _major_processor(queue, coordinator):
    provider = _FakeNoContentProvider("FLUSH_MAJOR\n- **Decisions made** - keep this")
    chosen_at = datetime(2026, 8, 16, 12, 34, 56, tzinfo=UTC)
    return _TimedCaptureProcessor(queue, coordinator, provider, chosen_at)


def _died_after_indexing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str = "status only", major: bool = False):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, text)
    processor = _NoContentProcessor(queue, coordinator, _FakeNoContentProvider())
    if major:
        processor = _major_processor(queue, coordinator)
    with monkeypatch.context() as patch, pytest.raises(RuntimeError, match="after the decision"):
        patch.setattr(flush_memory, "publish_capture_terminal", _crash_after_decision)
        flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET state='dead', lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
            (binding.task_id,),
        )
    return queue, coordinator, binding, processor


def _indexed_decisions(queue) -> int:
    with sqlite3.connect(queue.db_path) as database:
        return database.execute("SELECT count(*) FROM semantic_decisions").fetchone()[0]


def test_the_redrive_completes_on_the_decision_its_parent_indexed(tmp_path, monkeypatch) -> None:
    queue, coordinator, binding, processor = _died_after_indexing(tmp_path, monkeypatch)
    assert _indexed_decisions(queue) == 1
    queue.redrive(binding.task_id)

    relative = flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)

    assert relative == f"run/queue-results/capture-{binding.intent_id}.json"
    assert _indexed_decisions(queue) == 1


def test_the_redrive_writes_the_block_its_parent_wrote_only_once(tmp_path, monkeypatch) -> None:
    daily = tmp_path / "knowledge" / "daily" / "2026-08-16.md"
    daily.parent.mkdir(parents=True)
    queue, coordinator, binding, processor = _died_after_indexing(
        tmp_path, monkeypatch, "decision evidence", major=True
    )
    queue.redrive(binding.task_id)

    relative = flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)

    assert (relative, len(processor.provider.calls)) == (f"run/queue-results/capture-{binding.intent_id}.json", 1)
    _assert_major_daily_file_once(daily, binding.intent_id)


def test_a_decision_sealed_outside_the_redrive_chain_is_refused(tmp_path, monkeypatch) -> None:
    queue, _coordinator_, binding, _processor = _died_after_indexing(tmp_path, monkeypatch)
    child = queue.redrive(binding.task_id)
    with sqlite3.connect(queue.db_path) as database:
        database.row_factory = sqlite3.Row
        active = queue.active_capture_binding(database, child)
        with pytest.raises(queue_core.QueueOperationError, match="semantic_decision_conflict"):
            queue_core.require_inherited_capture_decision(
                database, {"task_id": "a-stranger"}, active,
                intent_id=binding.intent_id, stage="flush", active_link_digest=active.active_digest,
            )
