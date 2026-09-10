"""Typed captures bypass legacy processors and keep their own completion proof."""
from unittest.mock import Mock

import flush_memory
import integration_adapter
import pytest

from tests.test_capture_terminal import (
    _assert_completed_capture,
    _FakeNoContentProvider,
    _NoContentProcessor,
    _ready_intent_binding,
)
from tests.test_queue_v3_capture_links import _coordinator, _queue


def test_generic_claim_leaves_capture_for_terminal_worker(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    monkeypatch.setattr(flush_memory, "ROOT", tmp_path / "unused-session-vault")
    binding = _ready_intent_binding(queue, coordinator, queue.ownership_registry(), "status")
    assert queue.claim("legacy") is None
    assert queue.get(binding.task_id).attempts == 0
    processor = _NoContentProcessor(queue, coordinator, _FakeNoContentProvider())
    result = flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)
    _assert_completed_capture(queue, binding, result)


def test_generic_claim_selects_ordinary_work_after_capture(tmp_path):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    _ready_intent_binding(queue, coordinator, queue.ownership_registry(), "status")
    ordinary = queue.enqueue("query", 1, {"prompt": "ordinary"})
    lease = queue.claim("legacy")
    assert lease is not None
    assert lease.id == ordinary
    assert lease.attempt == 1


def test_capture_drain_processes_more_than_one_and_stops_when_empty(monkeypatch):
    work = Mock(side_effect=["terminal-one", "terminal-two", None])
    spawn = Mock()
    monkeypatch.setattr(integration_adapter, "spawn_detached", spawn)
    integration_adapter._drain_capture_work(work)
    assert work.call_count == 3
    spawn.assert_not_called()


def test_capture_drain_schedules_one_successor_at_task_limit(monkeypatch):
    work = Mock(return_value="terminal")
    spawn = Mock()
    monkeypatch.setattr(integration_adapter, "CAPTURE_DRAIN_MAX_TASKS", 2)
    monkeypatch.setattr(integration_adapter, "spawn_detached", spawn)
    integration_adapter._drain_capture_work(work)
    assert work.call_count == 2
    spawn.assert_called_once()
    assert spawn.call_args.args[0][-1] == "--capture-worker"


def test_capture_drain_schedules_successor_at_time_limit(monkeypatch):
    work = Mock(return_value="terminal")
    spawn = Mock()
    monkeypatch.setattr(integration_adapter.time, "monotonic", Mock(side_effect=[0, 451]))
    monkeypatch.setattr(integration_adapter, "spawn_detached", spawn)
    integration_adapter._drain_capture_work(work)
    work.assert_called_once()
    spawn.assert_called_once()


@pytest.mark.parametrize("reason", ["owner_busy", "provider_failed"])
def test_failed_capture_does_not_spawn_retry_chain(monkeypatch, reason):
    work = Mock(side_effect=RuntimeError(reason))
    spawn = Mock()
    monkeypatch.setattr(integration_adapter, "spawn_detached", spawn)
    with pytest.raises(RuntimeError, match=reason):
        integration_adapter._drain_capture_work(work)
    work.assert_called_once()
    spawn.assert_not_called()
