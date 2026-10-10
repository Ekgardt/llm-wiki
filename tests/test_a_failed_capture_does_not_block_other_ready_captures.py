"""A durably deferred task does not stop independent ready capture work."""
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import capture_diagnostics
import flush_memory
import integration_adapter
import markdown_transaction
import memory_queue
import pytest
from reliable_memory import DEFAULTS

from tests.test_capture_terminal import (
    _FakeNoContentProvider,
    _ready_intent_binding,
)
from tests.test_queue_v3_capture_links import _coordinator, _queue


def _first_fails(processor, calls, *args):
    calls.append(1)
    if len(calls) == 1:
        raise RuntimeError("first capture processor failed")
    return processor(*args)


def test_adapter_continues_after_a_committed_retry(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    first = _ready_intent_binding(queue, coordinator, queue.ownership_registry(), "first")
    second = _ready_intent_binding(queue, coordinator, queue.ownership_registry(), "second")
    processor = partial(flush_memory.process_new_capture, llm_call=_FakeNoContentProvider())
    calls = []
    diagnostic = Mock()
    # This scenario holds the failed task until its retry time; no real clock race.
    monkeypatch.setattr(memory_queue, "_utc_now", Mock(return_value=memory_queue._utc_now()))
    monkeypatch.setattr(
        memory_queue, "_RETRY_RANDOM",
        Mock(uniform=Mock(return_value=DEFAULTS.retry_base_seconds)),
    )
    monkeypatch.setattr(capture_diagnostics, "record_capture_failure", diagnostic)
    monkeypatch.setattr(integration_adapter, "ROOT", tmp_path)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(flush_memory, "ROOT", tmp_path)
    monkeypatch.setattr(memory_queue, "active_memory_queue", lambda *_args: queue)
    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", lambda *_args: coordinator)
    monkeypatch.setattr(flush_memory, "process_new_capture", partial(_first_fails, processor, calls))
    monkeypatch.setattr(integration_adapter, "spawn_detached", Mock())
    assert integration_adapter._run_active_capture_worker_once() == 0
    assert queue.get(first.task_id).state == "ready"
    assert queue.get(first.task_id).error_code == "processor_failed:RuntimeError"
    assert queue.get(second.task_id).state == "succeeded"
    assert len(calls) == 2
    diagnostic.assert_called_once()
    actual_error = diagnostic.call_args.kwargs["error"]
    assert str(actual_error) == "first capture processor failed"
    assert capture_diagnostics._outcome_of(actual_error, "adapter_capture_worker") == "deferred"


def test_failed_settlement_aborts_instead_of_observing_a_retry(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    _ready_intent_binding(queue, coordinator, queue.ownership_registry(), "first")
    error = RuntimeError("processor failed")
    settlement = RuntimeError("failure transition did not commit")
    observer = Mock()
    monkeypatch.setattr(type(queue), "fail", Mock(side_effect=settlement))
    with pytest.raises(RuntimeError) as raised:
        flush_memory.run_capture_worker_once(
            queue, coordinator, process_missing=Mock(side_effect=error),
            settled_failure=observer,
        )
    assert raised.value is settlement
    assert settlement.__context__ is error
    observer.assert_not_called()


def test_default_caller_receives_the_original_processor_exception(tmp_path):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, queue.ownership_registry(), "first")
    error = RuntimeError("processor failed")
    with pytest.raises(RuntimeError) as raised:
        flush_memory.run_capture_worker_once(queue, coordinator, process_missing=Mock(side_effect=error))
    assert raised.value is error
    assert queue.get(binding.task_id).state == "ready"


def test_exhausted_work_is_observed_as_lost_with_the_original_cause():
    original = RuntimeError("processor failed")
    lease = SimpleNamespace(attempt=DEFAULTS.queue_max_attempts)
    observer = Mock()
    result = flush_memory._settled_capture_failure(lease, original, observer)
    assert isinstance(result, capture_diagnostics.DurableWorkExhausted)
    assert result.__cause__ is original
    assert capture_diagnostics._outcome_of(result, "adapter_capture_worker") == "lost"
    observer.assert_called_once_with(result)


def test_observer_failure_still_aborts():
    original = RuntimeError("processor failed")
    diagnostic = OSError("cannot record failure")
    with pytest.raises(OSError) as raised:
        flush_memory._settled_capture_failure(
            SimpleNamespace(attempt=1), original, Mock(side_effect=diagnostic),
        )
    assert raised.value is diagnostic
