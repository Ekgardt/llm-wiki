"""A durable hook capture timed against the host while another writer holds the gate."""
import json
import threading
import time
from functools import partial, wraps

import breadcrumb_storage
import flush_memory
import integration_adapter as adapter
import markdown_transaction
import memory_state
import project_checkpoint
import queue_core
from llm_wiki import (
    event_project,
    integration_capture_records,
    integration_lifecycle,
    runtime_roots,
)
from project_journal import ProjectStore

from tests.adopted_capture_vault import adopted_capture_vault
from tests.slow_machine import LONG_TIMEOUT


def hold_writer(coordinator, held, release):
    with coordinator.writer_gate():
        held.set()
        assert release.wait(LONG_TIMEOUT)


def configure(tmp_path, monkeypatch):
    state_root, project = adopted_capture_vault(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", project))
    coordinator = markdown_transaction.active_markdown_coordinator(runtime_roots.vault_root(), state_root)
    return state_root, project, coordinator


def deliver(state_root):
    queue = queue_core.active_memory_queue(runtime_roots.vault_root(), state_root)
    coordinator = markdown_transaction.active_markdown_coordinator(runtime_roots.vault_root(), state_root)
    work = partial(flush_memory.run_capture_worker_once, queue, coordinator,
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator), handler_versions=(2,))
    assert work()
    assert work() is None


def timed_call(function, measurements, name):
    @wraps(function)
    def measured(*args, **kwargs):
        started = time.perf_counter()
        thread_started = time.thread_time()
        try:
            return function(*args, **kwargs)
        finally:
            entry = measurements.setdefault(name, {"calls": 0, "seconds": 0.0})
            entry["calls"] += 1
            entry["seconds"] += time.perf_counter() - started
            entry["thread_cpu_seconds"] = entry.get("thread_cpu_seconds", 0.0) + time.thread_time() - thread_started
    return measured


def time_ingestion_phases(timing, measurements):
    timing.setattr(integration_lifecycle, "publish_event_breadcrumb",
        timed_call(integration_lifecycle.publish_event_breadcrumb, measurements, "publish_event_breadcrumb"))
    timing.setattr(queue_core, "active_memory_queue",
        timed_call(queue_core.active_memory_queue, measurements, "active_memory_queue"))
    timing.setattr(markdown_transaction, "active_markdown_coordinator",
        timed_call(markdown_transaction.active_markdown_coordinator, measurements, "active_markdown_coordinator"))
    timing.setattr(breadcrumb_storage, "store_owned_bundle",
        timed_call(breadcrumb_storage.store_owned_bundle, measurements, "store_owned_bundle"))
    timing.setattr(breadcrumb_storage, "register_or_retain",
        timed_call(breadcrumb_storage.register_or_retain, measurements, "register_or_retain"))
    timing.setattr(integration_lifecycle, "after_breadcrumb_safely",
        timed_call(integration_lifecycle.after_breadcrumb_safely, measurements, "after_breadcrumb_safely"))
    timing.setattr(integration_capture_records, "wake_capture_worker",
        timed_call(integration_capture_records.wake_capture_worker, measurements, "wake_capture_worker"))


def measure_ingestion(envelope, monkeypatch):
    measurements = {}
    with monkeypatch.context() as timing:
        time_ingestion_phases(timing, measurements)
        started = time.monotonic()
        result = adapter.ingest_event(envelope)
        elapsed = time.monotonic() - started
    return result, elapsed, measurements


def capture_keeps_its_checkpoint_pending_without_waiting_past_host(tmp_path, monkeypatch, event):
    state_root, project, coordinator = configure(tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()
    writer = threading.Thread(target=hold_writer, args=(coordinator, held, release))

    wakes = []
    monkeypatch.setattr(integration_capture_records, "wake_capture_worker",
        lambda _result, intent_id: wakes.append(intent_id) and False)
    envelope = adapter.normalize_occurrence_event("claude", event, {
        "prompt": "continue", "tool_name": "Bash",
        "tool_input": {"command": "pwd"}, "cwd": str(project), "session_id": "prompt-gate",
        "event_id": "prompt-gate", "task_completed": True,
    })
    writer.start()
    try:
        # Establish the competing actor before the host's stopwatch. The real
        # wake operation spawns a worker without waiting for its writer gate.
        assert held.wait(LONG_TIMEOUT)
        result, elapsed, phases = measure_ingestion(envelope, monkeypatch)
        assert result["capture_durable"] is True
        assert elapsed < adapter.HOST_HOOK_TIMEOUT_SECONDS, json.dumps({
            "elapsed_seconds": elapsed, "nested_phases": phases,
            "registration_error": result.get("capture_registration_error"),
            "post_capture_error": result.get("post_capture_error"),
        }, sort_keys=True)
        assert list((state_root / "run/capture-intents/ready").rglob("*.json"))
        assert len(wakes) == phases["wake_capture_worker"]["calls"] >= 1
    finally:
        release.set()
        writer.join(LONG_TIMEOUT)
    assert not writer.is_alive()
    deliver(state_root)
    assert project_checkpoint.drain_pending_backlog()["failed"] == {}
    assert not memory_state.load_state()["project_checkpoint_pending"].get("demo")
    assert ProjectStore(runtime_roots.vault_root(), state_root).read_journal("demo")
