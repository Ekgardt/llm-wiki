"""A retained lifecycle event must finish its marker before semantic completion."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import daily_log_append
import flush_memory
import integration_adapter as adapter
import markdown_transaction
import pytest
import session_end_project_tag as tag
from markdown_transaction import active_markdown_coordinator
from memory_queue import active_memory_queue

from tests.adopted_capture_vault import adopted_capture_vault, host_transcript, intent_records
from tests.slow_machine import LONG_TIMEOUT
from tests.test_capture_terminal import _FakeNoContentProvider, _TimedCaptureProcessor

OCCURRED = datetime(2026, 10, 2, 23, 59, 58, tzinfo=timezone.utc)


def _event(project, transcript=None):
    return adapter.normalize_event(
        "claude", "session_end",
        {"session_id": "marker-replay", "event_id": "marker-event",
         "cwd": str(project), "reason": "logout", "transcript_path": transcript},
        occurred_at=OCCURRED,
    )


def _failed_tag(monkeypatch):
    failed = subprocess.CompletedProcess([], 1, "", "writer deadline reached")
    monkeypatch.setattr(adapter, "_run_delegate", lambda *args, **kwargs: failed)
    monkeypatch.setattr(adapter, "_wake_capture_worker", lambda *args: False)


def _work(state, monkeypatch):
    monkeypatch.setattr(flush_memory, "ROOT", adapter.ROOT)
    queue = active_memory_queue(adapter.ROOT, state)
    coordinator = active_markdown_coordinator(adapter.ROOT, state)
    provider = _FakeNoContentProvider()
    processor = _TimedCaptureProcessor(
        queue, coordinator, provider, datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    )
    result = flush_memory.run_capture_worker_once(queue, coordinator, process_missing=processor)
    return queue, result, provider


def _marker_text(vault):
    return "\n".join(path.read_text() for path in (vault / "knowledge/daily").glob("*.md"))


def test_failed_foreground_marker_is_restored_before_no_content_terminal(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    transcript = host_transcript(state, "marker.jsonl", '{"role":"user","content":"status"}\n')
    _failed_tag(monkeypatch)
    result = adapter.ingest_event(_event(project, str(transcript)))
    assert result["daily_log_written"] is False
    assert len(intent_records(state)) == 1
    queue, completed, provider = _work(state, monkeypatch)
    assert completed is not None
    assert provider.calls
    assert "- Project slug:" in _marker_text(adapter.ROOT)
    daily = adapter.ROOT / "knowledge/daily/2026-10-02.md"
    assert "[23:59:58] session-end | marker-replay" in daily.read_text()
    intent_id = intent_records(state)[0]["intent_id"]
    terminal = json.loads((state / f"run/queue-results/capture-{intent_id}.json").read_text())
    assert terminal["disposition"]["kind"] == "no_durable_content"
    assert queue.claim_capture("marker-check") is None


def test_missing_transcript_force_stub_keeps_real_metadata_for_replay(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    _failed_tag(monkeypatch)
    adapter.ingest_event(_event(project), force_stub=True)
    records = intent_records(state)
    assert len(records) == 1
    assert records[0]["evidence"][0]["role"] == "lifecycle"
    assert records[0]["session"] == "marker-replay"
    assert records[0]["occurred_at"] == OCCURRED.isoformat()
    _work(state, monkeypatch)
    assert "- Project slug:" in _marker_text(adapter.ROOT)


def test_missing_transcript_without_force_stub_stays_a_heartbeat(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    monkeypatch.setattr(adapter, "_record_activity", lambda *args: True)
    result = adapter.ingest_event(_event(project))
    assert result["heartbeat_recorded"] is True
    assert intent_records(state) == []
    assert _marker_text(adapter.ROOT) == ""


def _successful_tag(project, payload):
    written = tag._tag_project_payload(
        payload, adapter.ROOT, adapter.ROOT / "knowledge/daily", project
    )
    return subprocess.CompletedProcess([], 0, json.dumps({"daily_log_written": written}), "")


def test_worker_replay_keeps_one_exact_foreground_marker_after_midnight(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    transcript = host_transcript(state, "marker.jsonl", '{"role":"user","content":"status"}\n')
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setattr(adapter, "_wake_capture_worker", lambda *args: False)
    monkeypatch.setattr(adapter, "_run_delegate", lambda name, payload, **kwargs: _successful_tag(project, payload))
    result = adapter.ingest_event(_event(project, str(transcript)))
    assert result["daily_log_written"] is True
    before = _marker_text(adapter.ROOT)
    _work(state, monkeypatch)
    assert _marker_text(adapter.ROOT) == before
    assert before.count("session-end | marker-replay") == 1
    assert "[23:59:58]" in before
    assert not (adapter.ROOT / "knowledge/daily/2026-10-03.md").exists()


def _refuse_marker(*args, **kwargs):
    raise TimeoutError("writer deadline reached")


def test_worker_marker_failure_keeps_intent_without_false_terminal(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    _failed_tag(monkeypatch)
    adapter.ingest_event(_event(project), force_stub=True)
    intent_id = intent_records(state)[0]["intent_id"]
    monkeypatch.setattr(markdown_transaction, "append_owned_knowledge", _refuse_marker)
    with pytest.raises(TimeoutError, match="writer deadline"):
        _work(state, monkeypatch)
    assert not (state / f"run/queue-results/capture-{intent_id}.json").exists()
    assert intent_records(state)[0]["intent_id"] == intent_id
    assert _marker_text(adapter.ROOT) == ""


def test_direct_adapter_tag_delegate_uses_the_durable_force_stub_route(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    _failed_tag(monkeypatch)
    args = Namespace(delegate="session_end_project_tag.py", source="claude", background=False)
    adapter._dispatch_cli_event(args, _event(project))
    assert len(intent_records(state)) == 1
    _work(state, monkeypatch)
    assert "session-end | marker-replay" in _marker_text(adapter.ROOT)


@contextmanager
def _held_writer(state):
    code = (
        "import sys; from pathlib import Path; "
        "from markdown_transaction import active_markdown_coordinator; "
        "coordinator=active_markdown_coordinator(Path(sys.argv[1]),Path(sys.argv[2])); "
        "gate=coordinator.writer_gate(); gate.__enter__(); "
        "print('held',flush=True); sys.stdin.read(); gate.__exit__(None,None,None)"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(adapter.__file__).parent))
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(adapter.ROOT), str(state)], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "held"
        yield
    finally:
        _, error = process.communicate("", timeout=LONG_TIMEOUT)
    assert process.returncode == 0, error


def _deadline_delegate(project, payload):
    try:
        return _successful_tag(project, payload)
    except TimeoutError as error:
        return subprocess.CompletedProcess([], 1, "", str(error))


def test_actual_other_process_writer_cannot_lose_the_marker(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    transcript = host_transcript(state, "marker.jsonl", '{"role":"user","content":"status"}\n')
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setattr(daily_log_append, "LIFECYCLE_APPEND_BUDGET_SECONDS", 0.03)
    monkeypatch.setattr(adapter, "_wake_capture_worker", lambda *args: False)
    monkeypatch.setattr(adapter, "_run_delegate", lambda name, payload, **kwargs: _deadline_delegate(project, payload))
    with _held_writer(state):
        result = adapter.ingest_event(_event(project, str(transcript)))
        assert result["daily_log_written"] is False
        assert result["returncode"] == 1
        assert len(intent_records(state)) == 1
    _work(state, monkeypatch)
    assert "session-end | marker-replay" in _marker_text(adapter.ROOT)


def test_a_legacy_missing_clock_is_not_replaced_with_an_invented_time(tmp_path, monkeypatch):
    from reliable_memory import sha256_bytes

    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    source = adapter._capture_source_record(_event(project), "project", "logout", "status")
    source["occurred_at"] = None
    record, encoded = adapter._encoded_capture_record(source)
    intent_id = record["intent_id"]
    pending, ready = adapter._capture_relative_paths(intent_id)
    adapter._ensure_capture_intent_directories(state, intent_id)
    adapter._publish_capture_files_and_task(
        active_memory_queue(adapter.ROOT, state), active_markdown_coordinator(adapter.ROOT, state),
        intent_id=intent_id, payload=encoded, intent_sha256=sha256_bytes(encoded),
        pending_relative=pending, ready_relative=ready,
    )
    with pytest.raises(ValueError, match="no retained occurrence time"):
        _work(state, monkeypatch)
    assert intent_records(state)[0]["occurred_at"] is None
    assert not (state / f"run/queue-results/capture-{intent_id}.json").exists()


def test_forced_marker_inside_the_vault_remains_a_skip(tmp_path, monkeypatch):
    state, _project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    _failed_tag(monkeypatch)
    adapter.ingest_event(_event(adapter.ROOT), force_stub=True)
    assert intent_records(state) == []
    assert _marker_text(adapter.ROOT) == ""


def test_agent_worktree_marker_names_its_owning_checkout(tmp_path, monkeypatch):
    state, project = adopted_capture_vault(tmp_path, monkeypatch, adapter)
    worktree = project / ".claude/worktrees/worker"
    worktree.mkdir(parents=True)
    transcript = host_transcript(state, "marker.jsonl", '{"role":"user","content":"status"}\n')
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setattr(adapter, "_wake_capture_worker", lambda *args: False)
    monkeypatch.setattr(adapter, "_run_delegate", lambda name, payload, **kwargs: _successful_tag(project, payload))
    result = adapter.ingest_event(_event(worktree, str(transcript)))
    assert result["daily_log_written"] is True
    before = _marker_text(adapter.ROOT)
    _work(state, monkeypatch)
    assert _marker_text(adapter.ROOT) == before
    assert f"- Project root: `{project}`" in before
    assert str(worktree) not in before
