"""Guard tests for thin native lifecycle integrations.

These tests ensure that:
1. The OpenCode plugin forwards lifecycle events to shared Python ingestion.
2. Native integrations do not duplicate MCP reads or classification logic.
3. The Codex wrapper generates a context file before codex starts.
4. The Cursor rules file contains mandatory session-start context reading
5. The Antigravity AGENTS.md contains mandatory session-start context reading
6. session_start_context.py supports --output-file mode
7. The install scripts generate the initial context file

If any of these are removed, CI catches it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_opencode_plugin_is_lifecycle_only():
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert "integration_adapter.py" in plugin
    assert '"session.created"' in plugin
    assert '"tool.execute.after"' in plugin
    assert '"session.idle"' in plugin
    assert '"experimental.session.compacting"' in plugin
    assert '"memory.context"' not in plugin
    assert '"memory.recall"' not in plugin
    assert "Classify this transcript" not in plugin
    assert "FLUSH_MAJOR" not in plugin
    assert "memory-ephemeral" not in plugin
    assert "client.session.create" not in plugin
    assert "client.session.prompt" not in plugin
    assert "memory_queue.py" not in plugin
    assert "maybe_compile.py" not in plugin
    assert "computeSlug" not in plugin
    assert "state-path" not in plugin
    assert 'directory: typeof directory === "string" ? directory : null' in plugin
    assert "project" not in plugin


def test_normalization_preserves_only_available_checkpoint_signals():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from integration_adapter import normalize_event

    supplied = normalize_event(
        "opencode",
        "post_tool_use",
        {
            "sessionId": "session-1",
            "directory": "C:/project",
            "tool": "edit",
            "input": {"filePath": "src/app.py"},
            "event_id": "host-1",
            "dirty": True,
            "changed": True,
            "public_contract_changed": True,
            "token_percent": 70,
            "compaction_confirmed": True,
        },
    )
    unavailable = normalize_event(
        "codex",
        "session_end",
        {"session_id": "session-2", "cwd": "C:/project", "reason": "done"},
    )

    assert supplied.payload["dirty"] is True
    assert supplied.payload["changed"] is True
    assert supplied.payload["public_contract_changed"] is True
    assert supplied.payload["token_percent"] == 70
    assert supplied.payload["compaction_confirmed"] is True
    assert "token_percent" not in unavailable.payload
    assert "compaction_confirmed" not in unavailable.payload
    assert "host_progress_signals" not in unavailable.payload


def test_malformed_project_delta_is_rejected_before_durable_enqueue():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(ValueError, match="invalid project delta") as rejected:
        integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "project_delta": {
                    **integration_adapter._empty_delta(),
                    "blockers": [{"id": secret, "action": "append", "value": "bad"}],
                },
            },
        )
    assert secret not in str(rejected.value)


def test_significant_file_tool_normalizes_to_file_change_observation():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from integration_adapter import _checkpoint_observation, normalize_event

    write = normalize_event(
        "opencode",
        "post_tool_use",
        {
            "sessionId": "s1",
            "directory": "C:/project",
            "tool": "edit",
            "input": {"filePath": "src/app.py"},
        },
    )
    read = normalize_event(
        "opencode",
        "post_tool_use",
        {
            "sessionId": "s1",
            "directory": "C:/project",
            "tool": "read",
            "input": {"filePath": "src/app.py"},
        },
    )

    assert _checkpoint_observation(write)["type"] == "file_changed"
    assert _checkpoint_observation(write)["significant"] is True
    assert _checkpoint_observation(read)["type"] == "post_tool_use"
    assert "significant" not in _checkpoint_observation(read)


def test_host_tool_call_id_separates_repeated_mutations():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from integration_adapter import normalize_event

    raw = {
        "session_id": "session-1",
        "cwd": "C:/project",
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/app.py"},
    }
    first = normalize_event("claude", "post_tool_use", {**raw, "tool_use_id": "tool-1"})
    second = normalize_event("claude", "post_tool_use", {**raw, "tool_use_id": "tool-2"})

    assert first.source_event_id == "tool-1"
    assert first.event_id != second.event_id


def test_adapter_observes_same_envelope_once_before_delegate(monkeypatch):
    import io
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_observe_project_checkpoint",
        lambda envelope: calls.append(("observe", envelope)),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda *args, **kwargs: calls.append(("delegate", args[1])) or None,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"session_id": "s1", "cwd": "C:/project", "reason": "done"})),
    )

    assert integration_adapter.main(
        ["--source", "claude", "--event", "session_end", "--delegate", "session_end_capture.py"]
    ) == 0
    assert [name for name, _ in calls] == ["observe", "delegate"]
    assert calls[1][1]["event_id"] == calls[0][1].event_id
    assert calls[1][1]["occurrence_id"] == calls[0][1].payload["occurrence_id"]


@pytest.mark.parametrize(
    ("event_type", "raw", "reason"),
    [
        ("pre_compact", {"reason": "auto"}, "before_compaction"),
        ("session_start", {"source": "compact"}, "after_compaction"),
    ],
)
def test_repeated_unidentified_lifecycle_occurrences_checkpoint_separately(
    monkeypatch, event_type, raw, reason
):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    state = {}
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))
    event_raw = {"session_id": "s1", "cwd": "C:/project", **raw}

    first = integration_adapter.normalize_occurrence_event("claude", event_type, event_raw)
    second = integration_adapter.normalize_occurrence_event("claude", event_type, event_raw)
    integration_adapter._observe_project_checkpoint(first)
    integration_adapter._observe_project_checkpoint(second)

    assert first.event_id != second.event_id
    assert first.source_event_id == first.payload["occurrence_id"]
    assert second.source_event_id == second.payload["occurrence_id"]
    uuid.UUID(first.payload["occurrence_id"])
    uuid.UUID(second.payload["occurrence_id"])
    assert [checkpoint["reason"] for checkpoint in checkpoints] == [reason, reason]
    assert [checkpoint["occurrence_id"] for checkpoint in checkpoints] == [
        first.event_id,
        second.event_id,
    ]


def test_same_normalized_occurrence_is_checkpointed_once(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    state = {}
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))
    envelope = integration_adapter.normalize_occurrence_event(
        "claude",
        "pre_compact",
        {"session_id": "s1", "cwd": "C:/project", "reason": "auto"},
    )

    integration_adapter._observe_project_checkpoint(envelope)
    integration_adapter._observe_project_checkpoint(envelope)

    assert len(checkpoints) == 1
    assert checkpoints[0]["occurrence_id"] == envelope.event_id


def test_delegate_runs_when_checkpoint_observation_fails(monkeypatch, capsys):
    import io
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_observe_project_checkpoint",
        lambda envelope: (_ for _ in ()).throw(RuntimeError("x" * 2000)),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda *args, **kwargs: calls.append(args[0]) or None,
    )
    monkeypatch.setattr(
        integration_adapter,
        "_log_checkpoint_error",
        lambda error: calls.append(("logged", len(str(error)))),
        raising=False,
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"session_id": "s1", "cwd": "C:/project"})),
    )

    assert integration_adapter.main(
        ["--source", "codex", "--event", "session_end", "--delegate", "session_end_capture.py"]
    ) == 0
    assert calls == [("logged", 2000), "session_end_capture.py"]
    assert "capture skipped" not in capsys.readouterr().err


def test_checkpoint_error_text_is_single_line_redacted_and_bounded():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    text = integration_adapter._bounded_checkpoint_error(
        RuntimeError(f"first\nAuthorization: Bearer {secret}" + "x" * 2000)
    )

    assert "\n" not in text
    assert secret not in text
    assert len(text) <= integration_adapter.MAX_CHECKPOINT_ERROR_CHARS


def test_adapter_observes_before_direct_ingestion(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    envelope = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/project"}
    )
    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_observe_project_checkpoint",
        lambda observed: calls.append(("observe", observed.event_id)),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_record_activity",
        lambda *args: calls.append(("ingest", args[0].event_id)) or True,
    )
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))

    integration_adapter.ingest_event(envelope)
    assert calls == [("observe", envelope.event_id), ("ingest", envelope.event_id)]


def test_direct_ingestion_continues_when_checkpoint_observation_fails(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    envelope = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/project"}
    )
    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_observe_project_checkpoint",
        lambda observed: (_ for _ in ()).throw(RuntimeError("checkpoint failed")),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_log_checkpoint_error",
        lambda error: calls.append("logged"),
        raising=False,
    )
    monkeypatch.setattr(
        integration_adapter,
        "_record_activity",
        lambda *args: calls.append("ingested") or True,
    )
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))

    result = integration_adapter.ingest_event(envelope)
    assert calls == ["logged", "ingested"]
    assert result["heartbeat_recorded"] is True


def test_claude_stop_is_dirty_checkpoint_only_and_never_dispatches_session_end(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_observe_project_checkpoint",
        lambda envelope: calls.append(("observe", envelope.event_type)),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, *args, **kwargs: calls.append(("delegate", name)),
    )
    envelope = integration_adapter.normalize_event(
        "claude",
        "stop",
        {"session_id": "s1", "cwd": "C:/project", "dirty": True},
    )

    result = integration_adapter.ingest_event(envelope)

    assert envelope.event_type == "stop"
    assert integration_adapter._checkpoint_observation(envelope) == {
        "type": "stop",
        "event_id": envelope.event_id,
        "dirty": True,
    }
    assert calls == [("observe", "stop")]
    assert result["daily_log_written"] is False
    assert result["flush_spawned"] is False


def test_failed_checkpoint_does_not_persist_event_dedupe_and_retry_succeeds(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    state = {}
    attempts = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, *args):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError("temporary checkpoint failure")

    envelope = integration_adapter.normalize_event(
        "codex",
        "session_end",
        {"session_id": "s1", "cwd": "C:/project", "event_id": "end-1"},
        occurred_at=integration_adapter.datetime.fromisoformat("2026-07-13T12:00:00+00:00"),
    )
    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))

    with pytest.raises(RuntimeError, match="temporary checkpoint failure"):
        integration_adapter._observe_project_checkpoint(envelope)
    assert state.get("project_checkpoint_reducers", {}) == {}

    integration_adapter._observe_project_checkpoint(envelope)
    reducer_state = state["project_checkpoint_reducers"]["demo:s1"]
    assert envelope.event_id in reducer_state["observed_event_ids"]
    assert len(attempts) == 2


def test_concurrent_distinct_events_are_each_journaled_exactly_once(monkeypatch, tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import JOURNAL_HEADER, ProjectStore

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project_dir = tmp_path / "project"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    project_dir.mkdir()
    runtime_state = {}
    state_lock = threading.Lock()

    def update(mutator, **kwargs):
        with state_lock:
            mutator(runtime_state)
            return runtime_state

    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(
        integration_adapter, "_project_context", lambda event: ("demo", project_dir)
    )
    events = [
        integration_adapter.normalize_event(
            "codex",
            "session_end",
            {
                "session_id": "session-1",
                "cwd": str(project_dir),
                "event_id": f"event-{index}",
            },
        )
        for index in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(integration_adapter._observe_project_checkpoint, events))

    journal = ProjectStore(vault, state_root).read_journal("demo")
    records = [
        json.loads(line)
        for line in journal.removeprefix(JOURNAL_HEADER).splitlines()
        if line
    ]
    assert sorted(record["occurrence_id"] for record in records) == sorted(
        event.event_id for event in events
    )
    assert len(records) == 2


def test_project_lease_busy_event_remains_pending_until_next_observation(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import ProjectLeaseBusy

    state = {}
    attempts = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner):
            attempts.append(event["occurrence_id"])
            if len(attempts) == 1:
                raise ProjectLeaseBusy("busy")

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))
    first = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "one"}
    )
    second = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "two"}
    )

    with pytest.raises(ProjectLeaseBusy):
        integration_adapter._observe_project_checkpoint(first)
    pending = state["project_checkpoint_pending"]["demo"]
    assert [item["event_id"] for item in pending] == [first.event_id]

    integration_adapter._observe_project_checkpoint(second)
    assert attempts == [first.event_id, first.event_id, second.event_id]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_reducer_commit_failure_releases_pending_claim_for_retry(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    state = {}
    updates = 0
    checkpoints = []

    def update(mutator, **kwargs):
        nonlocal updates
        updates += 1
        if updates == 3:
            raise TimeoutError("commit state busy")
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner):
            checkpoints.append(event["occurrence_id"])

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))
    event = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/p", "event_id": "one"}
    )

    with pytest.raises(TimeoutError, match="commit state busy"):
        integration_adapter._observe_project_checkpoint(event)
    pending = state["project_checkpoint_pending"]["demo"][0]
    assert "claim_owner" not in pending

    integration_adapter._observe_project_checkpoint(event)
    assert checkpoints == [event.event_id, event.event_id]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_session_start_maintenance_does_not_debounce_or_drop_following_delta(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import CheckpointReducer

    previous = integration_adapter.datetime.fromisoformat("2026-07-13T11:59:29+00:00")
    session_start_at = integration_adapter.datetime.fromisoformat(
        "2026-07-13T12:00:00+00:00"
    )
    ordinary_event_at = integration_adapter.datetime.fromisoformat(
        "2026-07-13T12:00:01+00:00"
    )
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=previous).to_state()
        }
    }
    checkpoints = []
    delta = integration_adapter._empty_delta()
    delta["current_task"] = {
        "id": "task-1",
        "action": "upsert",
        "value": "Preserve this delta",
    }

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))
    start = integration_adapter.normalize_event(
        "claude",
        "session_start",
        {"session_id": "s1", "cwd": "C:/project", "event_id": "start-1"},
        occurred_at=session_start_at,
    )
    ordinary = integration_adapter.normalize_event(
        "claude",
        "post_tool_use",
        {
            "session_id": "s1",
            "cwd": "C:/project",
            "event_id": "event-1",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
            "checkpoint_type": "correction",
            "project_delta": delta,
        },
        occurred_at=ordinary_event_at,
    )

    integration_adapter._observe_project_checkpoint(start)
    integration_adapter._observe_project_checkpoint(ordinary)

    reducer_state = state["project_checkpoint_reducers"]["demo:s1"]
    assert reducer_state["last_checkpoint_at"] == ordinary_event_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert state["project_checkpoint_pending"]["demo"] == []
    assert len(checkpoints) == 1
    assert checkpoints[0]["occurrence_id"] == ordinary.event_id
    assert checkpoints[0]["delta"]["current_task"] == delta["current_task"]
    assert checkpoints[0]["delta"]["current_task_operations"] == [
        delta["current_task"]
    ]


def test_debounced_deltas_flush_in_order_on_later_observation_exactly_once(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import CheckpointReducer

    start = integration_adapter.datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=start).to_state()
        }
    }
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    def delta(**changes):
        value = integration_adapter._empty_delta()
        value.update(changes)
        return value

    def event(event_id, seconds, checkpoint_type=None, project_delta=None):
        raw = {
            "session_id": "s1",
            "cwd": "C:/project",
            "event_id": event_id,
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        }
        if checkpoint_type:
            raw["checkpoint_type"] = checkpoint_type
        if project_delta:
            raw["project_delta"] = project_delta
        return integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            raw,
            occurred_at=start + timedelta(seconds=seconds),
        )

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda envelope: ("demo", ROOT))
    correction = event(
        "correction-1",
        1,
        "correction",
        delta(
            current_task={"id": "task-1", "action": "upsert", "value": "First"}
        ),
    )
    blocker = event(
        "blocker-1",
        2,
        "blocker_opened",
        delta(
            blockers=[
                {"id": "blocker-1", "action": "upsert", "value": "Waiting"}
            ]
        ),
    )
    file_change = event(
        "file-1",
        3,
        "file_changed",
        delta(
            current_task={"id": "task-1", "action": "upsert", "value": "Latest"},
            blockers=[
                {"id": "blocker-1", "action": "close", "value": "Resolved"}
            ],
            changed_files=[
                {"id": "src/app.py", "action": "upsert", "value": "src/app.py"}
            ],
        ),
    )
    timer = event("timer-1", 31)

    for envelope in (correction, blocker, file_change):
        integration_adapter._observe_project_checkpoint(envelope)
    assert checkpoints == []
    assert [
        item["event_id"] for item in state["project_checkpoint_pending"]["demo"]
    ] == [correction.event_id, blocker.event_id, file_change.event_id]

    state = json.loads(json.dumps(state))
    integration_adapter._observe_project_checkpoint(timer)
    assert len(checkpoints) == 1
    merged = checkpoints[0]
    assert merged["delta"]["current_task"]["value"] == "Latest"
    assert merged["delta"]["blockers"] == [
        {"id": "blocker-1", "action": "close", "value": "Resolved"}
    ]
    assert merged["delta"]["changed_files"] == [
        {"id": "src/app.py", "action": "upsert", "value": "src/app.py"}
    ]
    assert merged["evidence_event_ids"] == [
        correction.event_id,
        blocker.event_id,
        file_change.event_id,
        timer.event_id,
    ]
    assert state["project_checkpoint_pending"]["demo"] == []

    state = json.loads(json.dumps(state))
    integration_adapter._observe_project_checkpoint(timer)
    assert len(checkpoints) == 1


def test_bypass_event_immediately_flushes_debounced_delta_once_after_restart(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import CheckpointReducer

    start = integration_adapter.datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=start).to_state()
        }
    }
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    def make_event(event_id, seconds, checkpoint_type, field, operation):
        project_delta = integration_adapter._empty_delta()
        project_delta[field] = operation
        return integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "session_id": "s1",
                "cwd": "C:/project",
                "event_id": event_id,
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "checkpoint_type": checkpoint_type,
                "project_delta": project_delta,
            },
            occurred_at=start + timedelta(seconds=seconds),
        )

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda envelope: ("demo", ROOT))
    correction = make_event(
        "correction-1",
        1,
        "correction",
        "current_task",
        {"id": "task-1", "action": "upsert", "value": "Keep me"},
    )
    decision = make_event(
        "decision-1",
        2,
        "decision",
        "decisions",
        [{"id": "decision-1", "action": "upsert", "value": "Flush now"}],
    )

    integration_adapter._observe_project_checkpoint(correction)
    assert checkpoints == []
    integration_adapter._observe_project_checkpoint(decision)

    assert len(checkpoints) == 1
    assert checkpoints[0]["reason"] == "decision"
    assert checkpoints[0]["delta"]["current_task"]["value"] == "Keep me"
    assert checkpoints[0]["delta"]["decisions"][0]["value"] == "Flush now"
    assert checkpoints[0]["evidence_event_ids"] == [
        correction.event_id,
        decision.event_id,
    ]
    assert state["project_checkpoint_pending"]["demo"] == []

    state = json.loads(json.dumps(state))
    integration_adapter._observe_project_checkpoint(decision)
    assert len(checkpoints) == 1


def test_bypass_flush_batches_205_pending_events_across_failure_and_restart(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import CheckpointReducer

    started = integration_adapter.datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    state = {
        "project_checkpoint_reducers": {
            "demo:s1": CheckpointReducer(last_checkpoint_at=started).to_state()
        },
        "project_checkpoint_pending": {"demo": []},
    }
    attempts = 0
    checkpoints = []
    event_ids = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("second batch interrupted")
            checkpoints.append(event)

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    for index in range(205):
        delta = integration_adapter._empty_delta()
        delta["current_task"] = {
            "id": f"task-{index}",
            "action": "upsert",
            "value": f"Task {index}",
        }
        delta["decisions"] = [
            {
                "id": f"decision-{index}",
                "action": "upsert",
                "value": f"Decision {index}",
            }
        ]
        envelope = integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "session_id": "s1",
                "cwd": "C:/project",
                "event_id": f"event-{index}",
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "checkpoint_type": "decision" if index == 204 else "correction",
                "project_delta": delta,
            },
            occurred_at=started + timedelta(seconds=2 if index == 204 else 1),
        )
        event_ids.append(envelope.event_id)
        state["project_checkpoint_pending"]["demo"].append(
            integration_adapter._pending_checkpoint(envelope, "demo", "demo:s1")
        )

    with pytest.raises(RuntimeError, match="second batch interrupted"):
        integration_adapter._drain_project_checkpoints("demo", "demo")
    assert len(checkpoints) == 1
    assert len(checkpoints[0]["evidence_event_ids"]) == 100
    assert len(state["project_checkpoint_pending"]["demo"]) == 105

    state = json.loads(json.dumps(state))
    integration_adapter._drain_project_checkpoints("demo", "demo")

    assert [len(item["evidence_event_ids"]) for item in checkpoints] == [100, 100, 5]
    assert [len(item["delta"]["current_task_operations"]) for item in checkpoints] == [
        100,
        100,
        5,
    ]
    assert all(len(item["delta"]["decisions"]) <= 100 for item in checkpoints)
    assert [event_id for item in checkpoints for event_id in item["evidence_event_ids"]] == event_ids
    assert state["project_checkpoint_pending"]["demo"] == []

    state = json.loads(json.dumps(state))
    integration_adapter._drain_project_checkpoints("demo", "demo")
    assert len(checkpoints) == 3


def test_single_oversized_valid_delta_splits_before_enqueue(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    state = {}
    checkpoints = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    class Store:
        def __init__(self, *args):
            pass

        def checkpoint(self, slug, event, owner, **kwargs):
            checkpoints.append(event)

    delta = integration_adapter._empty_delta()
    delta["decisions"] = [
        {"id": f"decision-{index}", "action": "upsert", "value": str(index)}
        for index in range(205)
    ]
    envelope = integration_adapter.normalize_event(
        "claude",
        "post_tool_use",
        {
            "session_id": "s1",
            "cwd": "C:/project",
            "event_id": "oversized-1",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
            "checkpoint_type": "decision",
            "project_delta": delta,
        },
    )
    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(integration_adapter, "_project_context", lambda event: ("demo", ROOT))

    integration_adapter._observe_project_checkpoint(envelope)

    assert [len(item["delta"]["decisions"]) for item in checkpoints] == [100, 100, 5]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_session_start_recovers_transactions_then_project_before_handoff(
    monkeypatch, tmp_path, capsys
):
    import io
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import session_start_project_state
    from project_journal import ProjectProjection

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    (vault / "knowledge/projects/demo/journal.md").write_text("journal", encoding="utf-8")
    project.mkdir()
    calls = []

    class Coordinator:
        def recover(self):
            calls.append("transactions")

    class Store:
        def __init__(self, vault_root, state_root):
            self.coordinator = Coordinator()

        def recover(self, slug, **kwargs):
            self.coordinator.recover()
            calls.append(("project", slug))

        def projection(self, slug):
            calls.append(("projection", slug))
            return ProjectProjection(project=slug, last_applied_sequence=7)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(session_start_project_state, "_compute_slug", lambda *args: "demo")
    monkeypatch.setattr(session_start_project_state, "ProjectStore", Store, raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert session_start_project_state.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [
        "transactions",
        ("project", "demo"),
        ("projection", "demo"),
    ]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "project:demo" in context
    assert "sequence:7" in context


def test_session_start_recovers_interrupted_first_checkpoint_before_journal_exists(
    monkeypatch, tmp_path, capsys
):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import session_start_project_state
    from project_journal import ProjectProjection

    vault = tmp_path / "vault"
    project = tmp_path / "project"
    project.mkdir()
    project_state = vault / "knowledge/projects/demo"
    project_state.mkdir(parents=True)
    calls = []

    class Coordinator:
        def recover(self):
            calls.append("transactions")

    class Store:
        def __init__(self, vault_root, state_root):
            self.coordinator = Coordinator()

        def recover(self, slug, **kwargs):
            self.coordinator.recover()
            calls.append(("project", slug))
            (project_state / "journal.md").write_text("recovered", encoding="utf-8")

        def projection(self, slug):
            calls.append(("projection", slug))
            return ProjectProjection(project=slug, last_applied_sequence=1)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(session_start_project_state, "_compute_slug", lambda *args: "demo")
    monkeypatch.setattr(session_start_project_state, "ProjectStore", Store)
    assert not (project_state / "journal.md").exists()
    assert session_start_project_state.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [
        "transactions",
        ("project", "demo"),
        ("projection", "demo"),
    ]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "project:demo" in context
    assert "sequence:1" in context


def test_opencode_session_start_appends_recovered_bounded_project_handoff(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import ProjectProjection

    calls = []

    class Coordinator:
        def recover(self):
            calls.append("transactions")

    class Store:
        def __init__(self, vault_root, state_root):
            self.coordinator = Coordinator()

        def recover(self, slug, **kwargs):
            self.coordinator.recover()
            calls.append(("project", slug))

        def projection(self, slug):
            calls.append(("projection", slug))
            return ProjectProjection(project=slug, last_applied_sequence=7)

    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        integration_adapter, "_project_context", lambda event: ("demo", Path("C:/project"))
    )
    monkeypatch.setattr(integration_adapter, "_record_activity", lambda *args: True)
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    monkeypatch.setattr(
        integration_adapter,
        "build_session_start_context",
        lambda: "# General memory\n",
    )
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    envelope = integration_adapter.normalize_event(
        "opencode",
        "session_start",
        {"sessionId": "s1", "directory": "C:/project"},
    )

    result = integration_adapter.ingest_event(envelope)

    assert calls == [
        "transactions",
        ("project", "demo"),
        ("projection", "demo"),
    ]
    assert "# General memory" in result["context"]
    assert "project:demo" in result["context"]
    assert "sequence:7" in result["context"]


def test_opencode_node_injects_shared_bounded_legacy_handoff_for_unicode_slug(
    monkeypatch, tmp_path
):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import ProjectStore, recover_project_handoff

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "проект"
    slug = "проект"
    project.mkdir()
    project_state = vault / "knowledge/projects" / slug
    project_state.mkdir(parents=True)
    fixture = ROOT / "tests/fixtures/project-state-older.md"
    (project_state / "state.md").write_text(
        fixture.read_text(encoding="utf-8").replace("{PROJECT_ROOT}", str(project)),
        encoding="utf-8",
    )
    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(integration_adapter, "_record_activity", lambda *args: True)
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    monkeypatch.setattr(integration_adapter, "build_session_start_context", lambda: "")
    envelope = integration_adapter.normalize_event(
        "opencode",
        "session_start",
        {"sessionId": "unicode-session", "directory": str(project)},
    )

    started = time.perf_counter()
    result = integration_adapter.ingest_event(envelope)
    elapsed = time.perf_counter() - started
    shared = recover_project_handoff(
        ProjectStore(vault, state_root), slug, project_root=project
    )

    assert elapsed < 0.25
    assert result["context"] == shared.context
    assert len(result["context"]) <= 2400
    assert "Preserve older handoff" in result["context"]
    assert "Preserve older open thread" in result["context"]
    assert f"project:{slug}" in result["context"]
    assert not (project_state / "journal.md").exists()

    plugin_url = (ROOT / "scripts/llm-wiki-memory-opencode.js").resolve().as_uri()
    script = textwrap.dedent(
        f"""
        globalThis.Bun = {{ spawn() {{
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            stdout: new Blob([{json.dumps(json.dumps({'context': result['context']}))}]).stream(),
            exited: Promise.resolve(0),
            kill() {{}},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)});
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: {json.dumps(str(project))} }});
        await hooks["session.created"]({{ sessionInfo: {{ id: "unicode-session" }} }});
        const output = {{ system: [] }};
        await hooks["experimental.chat.system.transform"](
          {{ sessionInfo: {{ id: "unicode-session" }} }}, output
        );
        console.log(JSON.stringify(output.system));
        """
    )
    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(vault)
    node = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=5,
    )
    assert node.returncode == 0, node.stderr
    assert json.loads(node.stdout) == [result["context"]]


def test_opencode_session_start_project_recovery_is_fail_open(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    class Store:
        def __init__(self, *args):
            raise RuntimeError("recovery unavailable")

    errors = []
    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        integration_adapter, "_project_context", lambda event: ("demo", Path("C:/project"))
    )
    monkeypatch.setattr(integration_adapter, "_record_activity", lambda *args: True)
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    monkeypatch.setattr(
        integration_adapter,
        "build_session_start_context",
        lambda: "# General memory\n",
    )
    monkeypatch.setattr(integration_adapter, "ProjectStore", Store)
    monkeypatch.setattr(
        integration_adapter, "_log_checkpoint_error", lambda error: errors.append(str(error))
    )
    envelope = integration_adapter.normalize_event(
        "opencode", "session_start", {"directory": "C:/project"}
    )

    result = integration_adapter.ingest_event(envelope)

    assert result["context"] == "# General memory\n"
    assert errors == ["recovery unavailable"]


def test_opencode_session_start_writer_contention_is_bounded_and_degraded(
    monkeypatch, tmp_path
):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import ProjectStore

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project_dir = tmp_path / "project"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    project_dir.mkdir()
    store = ProjectStore(vault, state_root)
    held = threading.Event()

    def hold_writer():
        with store.coordinator.writer_gate():
            held.set()
            time.sleep(1)

    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        integration_adapter, "_project_context", lambda event: ("demo", project_dir)
    )
    monkeypatch.setattr(integration_adapter, "_record_activity", lambda *args: True)
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    monkeypatch.setattr(
        integration_adapter, "build_session_start_context", lambda: "# General memory\n"
    )
    envelope = integration_adapter.normalize_event(
        "opencode", "session_start", {"directory": str(project_dir)}
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_writer)
        assert held.wait(2)
        started = time.perf_counter()
        result = integration_adapter.ingest_event(envelope)
        elapsed = time.perf_counter() - started
        holder.result(timeout=5)

    assert elapsed < 0.75
    assert "# General memory" in result["context"]
    assert "Degraded" in result["context"]
    assert "project:demo" in result["context"]
    assert "recovery:project:demo" in result["context"]


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_claude_and_codex_project_state_are_bounded_under_writer_contention(
    host, tmp_path
):
    import os
    import shutil
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import ProjectStore

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project_dir = tmp_path / "demo"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    project_dir.mkdir()
    store = ProjectStore(vault, state_root)
    store.checkpoint(
        "demo",
        {
            "schema_version": "project-checkpoint/v1",
            "occurrence_id": "committed-context",
            "idempotency_key": "committed-context",
            "provenance": {
                "agent": "test",
                "session": "test",
                "worktree": str(project_dir),
                "branch": "test",
                "source_event": "test",
            },
            "trigger": "decision",
            "reason": "committed context",
            "delta": integration_adapter._empty_delta(),
            "evidence_event_ids": ["test"],
        },
        "test",
    )
    held = threading.Event()

    def hold_writer():
        with store.coordinator.writer_gate():
            held.set()
            time.sleep(2)

    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(vault)
    env["LLM_WIKI_STATE_ROOT"] = str(state_root)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    command = [sys.executable, str(ROOT / "scripts/session_start_project_state.py")]
    if host == "codex":
        shutil.copytree(ROOT / "scripts", vault / "scripts")
        command = [
            sys.executable,
            str(ROOT / "scripts/codex_memory.py"),
            "project-state",
            "--cwd",
            str(project_dir),
            "--json",
        ]

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_writer)
        assert held.wait(2)
        started = time.perf_counter()
        result = subprocess.run(
            command,
            cwd=str(vault),
            env=env,
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        elapsed = time.perf_counter() - started
        holder.result(timeout=5)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    if host == "claude":
        context = payload["hookSpecificOutput"]["additionalContext"]
    else:
        context = payload["additional_context"]
    assert elapsed < 1.5
    assert len(context) <= 2400
    assert "project:demo" in context
    assert "sequence:1" in context
    assert "Degraded" in context
    assert "recovery:project:demo" in context


def test_claude_hooks_cover_compaction_failure_stop_and_end_signals():
    settings = json.loads(
        (ROOT / "integrations" / "claude-code" / "settings.json").read_text(encoding="utf-8")
    )
    hooks = settings["hooks"]
    assert {"SessionStart", "PreCompact", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"} <= set(hooks)
    stop_command = hooks["Stop"][0]["hooks"][0]["command"]
    failure_command = hooks["PostToolUseFailure"][0]["hooks"][0]["command"]
    assert "--event stop" in stop_command
    assert "--event session_end" not in stop_command
    assert "--delegate" not in stop_command
    assert "--checkpoint-type significant_failure" in failure_command


def test_claude_session_start_uses_one_outer_adapter_budget():
    settings = json.loads(
        (ROOT / "integrations/claude-code/settings.json").read_text(encoding="utf-8")
    )
    hooks = settings["hooks"]["SessionStart"][0]["hooks"]

    assert len(hooks) == 1
    command = hooks[0]["command"]
    assert "integration_adapter.py" in command
    assert "--event session_start" in command
    assert "--delegate" not in command


def test_claude_outer_session_start_preserves_hook_output_contract(
    monkeypatch, capsys
):
    import io
    import sys

    import integration_adapter

    monkeypatch.setattr(
        integration_adapter,
        "ingest_event",
        lambda _envelope: {"context": "combined context\n"},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert integration_adapter.main(
        ["--source", "claude", "--event", "session_start"]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "combined context\n",
        }
    }


def test_claude_session_end_uses_one_adapter_occurrence_for_both_side_effects(monkeypatch):
    import io
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    settings = json.loads(
        (ROOT / "integrations/claude-code/settings.json").read_text(encoding="utf-8")
    )
    hooks = settings["hooks"]["SessionEnd"][0]["hooks"]
    assert len(hooks) == 1
    assert "--event session_end" in hooks[0]["command"]
    assert "--delegate" not in hooks[0]["command"]

    calls = []
    monkeypatch.setattr(
        integration_adapter,
        "_observe_project_checkpoint",
        lambda envelope: calls.append(("observe", envelope.event_id)),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: calls.append(("delegate", name))
        or type("Result", (), {"returncode": 0, "stdout": '{"flush_started": true}'})(),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "s1",
                    "cwd": "C:/project",
                    "transcript_path": "session.jsonl",
                }
            )
        ),
    )

    assert integration_adapter.main(["--source", "claude", "--event", "session_end"]) == 0
    assert [name for kind, name in calls if kind == "delegate"] == [
        "session_end_project_tag.py",
        "session_end_capture.py",
    ]
    assert len([item for item in calls if item[0] == "observe"]) == 1


def test_codex_wrapper_recovers_project_state_before_launch():
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    recovery = wrapper.index("codex_memory.py project-state")
    launch = wrapper.index("& $REAL_CODEX @fwdArgs")
    assert recovery < launch


def test_codex_official_hook_template_matches_supported_contract():
    template = json.loads(
        (ROOT / "integrations" / "codex" / "hooks.json").read_text(encoding="utf-8")
    )
    hooks = template["hooks"]
    assert set(hooks) == {"SessionStart", "PreCompact", "PostCompact", "Stop"}
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    assert hooks["PreCompact"][0]["matcher"] == "manual|auto"
    assert hooks["PostCompact"][0]["matcher"] == "manual|auto"
    assert "matcher" not in hooks["Stop"][0]
    for groups in hooks.values():
        for group in groups:
            assert len(group["hooks"]) == 1
            command = group["hooks"][0]
            assert command["type"] == "command"
            assert "codex_memory.py" in command["command"]
            assert command["command"].endswith(" hook")
            assert "codex_memory.py" in command["commandWindows"]
            assert command["commandWindows"].endswith(" hook")
            assert 0 < command["timeout"] <= 15


def test_codex_hook_merge_preserves_user_hooks_and_is_idempotent(tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    destination = tmp_path / "hooks.json"
    destination.write_text(
        json.dumps(
            {
                "custom": {"preserved": True},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "echo user"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old scripts/codex_memory.py hook",
                                }
                            ]
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    codex_memory.merge_codex_hooks(source, destination)
    first = destination.read_bytes()
    codex_memory.merge_codex_hooks(source, destination)
    merged = json.loads(destination.read_text(encoding="utf-8"))

    assert destination.read_bytes() == first
    assert merged["custom"] == {"preserved": True}
    assert any(
        hook.get("command") == "echo user"
        for group in merged["hooks"]["Stop"]
        for hook in group["hooks"]
    )
    ours = [
        hook
        for groups in merged["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if "codex_memory.py" in hook.get("command", "")
    ]
    assert len(ours) == 4


def _codex_inline_hooks_toml(*, include_stop: bool = True) -> str:
    template = json.loads(
        (ROOT / "integrations" / "codex" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    blocks = []
    for event_name, groups in template.items():
        if event_name == "Stop" and not include_stop:
            continue
        group = groups[0]
        blocks.append(f"[[hooks.{event_name}]]")
        if "matcher" in group:
            blocks.append(f'matcher = {json.dumps(group["matcher"])}')
        handler = group["hooks"][0]
        blocks.extend(
            [
                "",
                f"[[hooks.{event_name}.hooks]]",
                'type = "command"',
                f'command = {json.dumps(handler["command"])}',
                f'command_windows = {json.dumps(handler["commandWindows"])}',
                f'timeout = {handler["timeout"]}',
                "",
            ]
        )
    return "\n".join(blocks)


def test_codex_hook_merge_skips_equivalent_inline_hooks(tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(), encoding="utf-8")
    destination.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    before = destination.read_bytes()

    result = codex_memory.merge_codex_hooks(source, destination, config=config)

    assert result == "inline-equivalent"
    assert destination.read_bytes() == before


def test_codex_hook_command_reports_equivalent_inline_without_creating_json(
    tmp_path, capsys
):
    import argparse
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(), encoding="utf-8")

    result = codex_memory.command_merge_hooks(
        argparse.Namespace(source=str(source), destination=str(destination), config=str(config))
    )

    assert result == 3
    assert capsys.readouterr().out.strip() == "inline-equivalent"
    assert not destination.exists()


def test_codex_hook_merge_preserves_json_when_inline_is_equivalent(tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(), encoding="utf-8")
    installed = json.loads(source.read_text(encoding="utf-8"))
    installed["custom"] = True
    installed["hooks"]["Stop"].insert(
        0, {"hooks": [{"type": "command", "command": "echo user"}]}
    )
    destination.write_text(json.dumps(installed), encoding="utf-8")

    result = codex_memory.merge_codex_hooks(source, destination, config=config)

    assert result == "inline-equivalent"
    assert json.loads(destination.read_text(encoding="utf-8")) == installed


def test_codex_hook_merge_rejects_unrelated_inline_hooks_without_creating_json(tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "echo user"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manual merge and trust review required"):
        codex_memory.merge_codex_hooks(source, destination, config=config)

    assert not destination.exists()


def test_codex_hook_merge_rejects_partial_inline_hooks_without_writing(tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(include_stop=False), encoding="utf-8")
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()

    with pytest.raises(ValueError, match="manual merge and trust review required"):
        codex_memory.merge_codex_hooks(source, destination, config=config)

    assert destination.read_bytes() == before


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ("hooks = false", "disabled"),
        ("codex_hooks = false", "disabled"),
        ("hooks = true\ncodex_hooks = false", "enabled"),
        ("hooks = false\ncodex_hooks = true", "disabled"),
        ("hooks = true", "enabled"),
        ("", "enabled"),
    ],
    ids=[
        "canonical-disabled",
        "alias-disabled",
        "canonical-enabled-wins",
        "canonical-disabled-wins",
        "canonical-enabled",
        "default-enabled",
    ],
)
def test_codex_hooks_feature_state_obeys_canonical_precedence(
    tmp_path, features, expected
):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    config = tmp_path / "config.toml"
    body = f"[features]\n{features}\n" if features else 'model = "gpt-5.6"\n'
    config.write_text(body, encoding="utf-8")

    assert codex_memory.codex_hooks_feature_state(config) == expected


def test_codex_hook_command_reports_disabled_without_writing(tmp_path, capsys):
    import argparse
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text("[features]\nhooks = false\n", encoding="utf-8")

    result = codex_memory.command_merge_hooks(
        argparse.Namespace(source=str(source), destination=str(destination), config=str(config))
    )

    assert result == 4
    assert capsys.readouterr().out.strip() == "hooks-disabled"
    assert not destination.exists()


@pytest.mark.parametrize("quoted", [False, True], ids=["dotted", "quoted"])
def test_codex_mcp_config_state_accepts_exact_enabled_table(tmp_path, quoted):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    config = tmp_path / "config.toml"
    table = '[mcp_servers."llm-wiki"]' if quoted else "[mcp_servers.llm-wiki]"
    config.write_text(
        f'{table}\ncommand = "uv"\n'
        f'args = ["run", "--directory", {json.dumps(str(ROOT))}, '
        '"python", "scripts/mcp_server.py"]\nenabled = true\n',
        encoding="utf-8",
    )

    assert codex_memory.codex_mcp_config_state(config, ROOT) == "equivalent"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('model = "gpt-5.6"\n', "absent"),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
            'args = ["python", "other.py"]\n',
            "conflict",
        ),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
            'args = ["run", "--directory", "wrong", "python", '
            '"scripts/mcp_server.py"]\nenabled = false\n',
            "conflict",
        ),
    ],
)
def test_codex_mcp_config_state_distinguishes_absent_and_conflicting(
    tmp_path, body, expected
):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")

    assert codex_memory.codex_mcp_config_state(config, ROOT) == expected


def test_codex_installers_use_official_hooks_and_request_trust_review():
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

    for installer in (shell, powershell):
        assert "integrations/codex/hooks.json" in installer.replace("\\", "/")
        assert "codex_memory.py" in installer
        assert "merge-hooks" in installer
        assert "--config" in installer
        assert "/hooks" in installer
        assert "trust" in installer.casefold()
    assert "not installed automatically" in powershell.casefold()


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", source)
    assert match, f"{name} missing"
    return match.group(0)


def _codex_mcp_toml(vault: Path, *, quoted: bool, conflicting: bool = False) -> str:
    table = '[mcp_servers."llm-wiki"]' if quoted else "[mcp_servers.llm-wiki]"
    args = ["python", "other.py"] if conflicting else [
        "run",
        "--directory",
        str(vault),
        "python",
        "scripts/mcp_server.py",
    ]
    return f'{table}\ncommand = "uv"\nargs = {json.dumps(args)}\n'


@pytest.mark.parametrize(
    ("scenario", "expected_exit"),
    [("equivalent", 0), ("conflicting", 2), ("absent", 0)],
    ids=["quoted-equivalent", "conflicting", "absent"],
)
def test_unix_installer_mcp_function_uses_parser_in_temp_home(
    tmp_path, scenario, expected_exit
):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _shell_function(source, "configure_codex_mcp")
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = (
        'model = "gpt-5.6"\n'
        if scenario == "absent"
        else _codex_mcp_toml(
            ROOT, quoted=True, conflicting=scenario == "conflicting"
        )
    )
    config.write_text(original, encoding="utf-8")
    before = config.read_bytes()
    runner = tmp_path / "installer-mcp-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != config-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'configure_codex_mcp "$TEST_VAULT" "$HOME/.codex/config.toml"\n'
        + "exit $?\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    if scenario == "absent":
        assert config.read_text(encoding="utf-8").startswith(original)
        assert config.read_bytes() != before
    else:
        assert config.read_bytes() == before
    assert config.read_text(encoding="utf-8").count("[mcp_servers.") == 1


@pytest.mark.parametrize(
    ("scenario", "expected_exit"),
    [("equivalent", 0), ("conflicting", 2), ("absent", 0)],
    ids=["quoted-equivalent", "conflicting", "absent"],
)
def test_windows_installer_mcp_function_uses_parser_in_temp_home(
    tmp_path, scenario, expected_exit
):
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = (
        'model = "gpt-5.6"\n'
        if scenario == "absent"
        else _codex_mcp_toml(
            ROOT, quoted=True, conflicting=scenario == "conflicting"
        )
    )
    config.write_text(original, encoding="utf-8")
    before = config.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Install-CodexMcp'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Install-CodexMcp missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'config-state')
            if ($index -lt 0) {{ throw 'config-state missing' }}
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / 'scripts/codex_memory.py'))} $all[$index..($all.Count - 1)]
        }}
        $code = Install-CodexMcp -VaultRoot {json.dumps(str(ROOT))} -Config {json.dumps(str(config))}
        exit $code
        """
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    if scenario == "absent":
        assert config.read_text(encoding="utf-8").startswith(original)
        assert config.read_bytes() != before
    else:
        assert config.read_bytes() == before
    assert config.read_text(encoding="utf-8").count("[mcp_servers.") == 1


def test_unix_installer_hook_function_executes_in_temp_home(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _shell_function(source, "install_codex_hooks")
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    (codex_dir / "hooks.json").write_text(
        '{"custom":true,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    runner = tmp_path / "installer-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != merge-hooks ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\n"
        + 'install_codex_hooks "$TEST_VAULT" "$HOME/.codex"\n'
        + 'install_codex_hooks "$TEST_VAULT" "$HOME/.codex"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    merged = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
    assert merged["custom"] is True
    assert sum(
        "codex_memory.py" in handler.get("command", "")
        for groups in merged["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ) == 4
    assert any(
        handler.get("command") == "echo user"
        for group in merged["hooks"]["Stop"]
        for handler in group["hooks"]
    )


def test_unix_installer_preserves_json_when_unrelated_inline_hooks_exist(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    function = _shell_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "install_codex_hooks"
    )
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "echo user"\n',
        encoding="utf-8",
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    runner = tmp_path / "installer-inline-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != merge-hooks ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'install_codex_hooks "$TEST_VAULT" "$HOME/.codex"\n'
        + "exit $?\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 2
    assert "manual merge and trust review" in result.stderr
    assert destination.read_bytes() == before


def test_windows_installer_hook_function_executes_in_temp_home(tmp_path):
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    (codex_dir / "hooks.json").write_text(
        '{"custom":true,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Install-CodexHooks'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Install-CodexHooks missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'merge-hooks')
            if ($index -lt 0) {{ throw 'merge-hooks missing' }}
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / 'scripts/codex_memory.py'))} $all[$index..($all.Count - 1)]
        }}
        Install-CodexHooks -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
        Install-CodexHooks -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        exit $LASTEXITCODE
        """
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    merged = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
    assert merged["custom"] is True
    assert sum(
        "codex_memory.py" in handler.get("command", "")
        for groups in merged["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ) == 4
    assert any(
        handler.get("command") == "echo user"
        for group in merged["hooks"]["Stop"]
        for handler in group["hooks"]
    )


def test_windows_installer_preserves_json_when_partial_inline_hooks_exist(tmp_path):
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        _codex_inline_hooks_toml(include_stop=False), encoding="utf-8"
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Install-CodexHooks'
        }}, $true)
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'merge-hooks')
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / 'scripts/codex_memory.py'))} $all[$index..($all.Count - 1)]
        }}
        $code = Install-CodexHooks -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        exit $code
        """
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 2
    assert "manual merge and trust review" in result.stderr
    assert destination.read_bytes() == before


def test_unix_installer_warns_and_preserves_json_when_hooks_feature_disabled(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    function = _shell_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "install_codex_hooks"
    )
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[features]\ncodex_hooks = false\n", encoding="utf-8"
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    runner = tmp_path / "installer-disabled-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != merge-hooks ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'install_codex_hooks "$TEST_VAULT" "$HOME/.codex"\n'
        + "exit $?\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(HOME=str(home), TEST_VAULT=str(ROOT), TEST_PYTHON=sys.executable)

    result = subprocess.run(
        [str(bash), str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 4
    assert "hooks = true" in result.stderr
    assert destination.read_bytes() == before


def test_windows_installer_warns_and_preserves_json_when_hooks_feature_disabled(
    tmp_path,
):
    source = ROOT / "install.ps1"
    codex_dir = tmp_path / "home" / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[features]\nhooks = false\ncodex_hooks = true\n", encoding="utf-8"
    )
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(source))}, [ref]$tokens, [ref]$errors)
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Install-CodexHooks'
        }}, $true)
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'merge-hooks')
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / 'scripts/codex_memory.py'))} $all[$index..($all.Count - 1)]
        }}
        $code = Install-CodexHooks -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        exit $code
        """
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 4
    assert "hooks = true" in result.stderr
    assert destination.read_bytes() == before


def test_codex_wrapper_is_labeled_compatibility_heartbeat_fallback():
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    assert "compatibility fallback" in wrapper.casefold()
    assert "heartbeat-only" in wrapper.casefold()
    assert "official hooks" in wrapper.casefold()


def test_opencode_host_directory_maps_directly_to_worktree_or_null():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from integration_adapter import normalize_event

    supplied = normalize_event(
        "opencode",
        "session_start",
        {"directory": "C:/host/project"},
    )
    unavailable = normalize_event(
        "opencode",
        "session_start",
        {"directory": None},
    )

    assert supplied.worktree == "C:/host/project"
    assert unavailable.worktree is None


def test_opencode_node_harness_forwards_bounded_tail_and_escaped_paths(tmp_path):
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
    root = str(tmp_path / "Vault With Spaces")
    directory = str(tmp_path / "Project With Spaces")
    adapter_context = json.dumps({
        "context": (
            "# Project memory context\n\n## Health\n\nScheduler degraded.\n\n"
            "# Project handoff\n\n## MCP identifiers\n"
            "- `project:demo`\n- `sequence:7`\n"
        )
    })
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        process.env.LLM_WIKI_CAPTURE_TIMEOUT_MS = "20";
        const commands = [];
        const requests = [];
        globalThis.Bun = {{ spawn(args, options) {{
          const record = {{ args, options, stdin: "", killed: false }};
          commands.push(record);
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          const context = args.at(-1) === "session_start"
            ? {json.dumps(adapter_context)}
            : "";
          const stdout = new ReadableStream({{ start(controller) {{
            if (context) controller.enqueue(new TextEncoder().encode(context));
            controller.close();
          }} }});
          return {{
            stdin: {{
              write(value) {{ record.stdin += value; }},
              end() {{ finish(0); }},
            }},
            stdout,
            exited,
            kill() {{ record.killed = true; finish(143); }},
          }};
        }} }};
        const client = {{ session: {{ messages: async (request) => {{
          requests.push(request);
          return {{ data: Array.from({{ length: 20 }}, (_, i) => ({{
            parts: [{{ text: `m${{i}}-` + "x".repeat(100) }}],
          }})) }};
        }} }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=1");
        const hooks = await LlmWikiMemoryPlugin({{ client, directory: {json.dumps(directory)} }});
        await hooks["session.idle"]({{ sessionId: "session-1" }});
        await hooks["experimental.session.compacting"]({{ sessionId: "session-1" }});
        await hooks["session.created"]({{ sessionInfo: {{ id: "session-1" }} }});
        const system = [];
        await hooks["experimental.chat.system.transform"](
          {{ sessionID: "session-1" }}, {{ system }}
        );
        console.log(JSON.stringify({{ commands, requests, system }}));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    payload = json.loads(observed["commands"][0]["stdin"])
    assert payload["directory"] == directory
    assert payload["transcript_text"].startswith("m8-")
    assert "m7-" not in payload["transcript_text"]
    assert len(payload["transcript_text"]) <= 8000
    assert observed["requests"][0]["query"]["limit"] == 12
    assert observed["commands"][1]["stdin"]
    compact_payload = json.loads(observed["commands"][1]["stdin"])
    assert compact_payload["transcript_text"].startswith("m8-")
    assert "## Health" in observed["system"][0]
    assert "project:demo" in observed["system"][0]
    assert "sequence:7" in observed["system"][0]
    assert observed["commands"][0]["args"] == [
        "uv",
        "run",
        "--directory",
        root,
        "python",
        f"{root}/scripts/integration_adapter.py",
        "--source",
        "opencode",
        "--event",
        "session_end",
    ]


def test_opencode_node_harness_times_out_stalled_capture(tmp_path):
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
    root = str(tmp_path / "Vault With Spaces")
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        process.env.LLM_WIKI_CAPTURE_TIMEOUT_MS = "20";
        let killed = false;
        globalThis.Bun = {{ spawn() {{
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            exited,
            kill() {{ killed = true; finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=timeout");
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "project" }});
        const started = Date.now();
        await hooks["session.created"]({{}});
        console.log(JSON.stringify({{ elapsed: Date.now() - started, killed }}));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["elapsed"] < 500
    assert observed["killed"] is True


def test_opencode_forwards_known_mutation_and_dirty_idle_without_fake_progress_signals(tmp_path):
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
    root = str(tmp_path / "vault")
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        const payloads = [];
        globalThis.Bun = {{ spawn() {{
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{
              write(value) {{ payloads.push(JSON.parse(value)); }},
              end() {{ finish(0); }},
            }},
            stdout: new ReadableStream({{ start(controller) {{ controller.close(); }} }}),
            exited,
            kill() {{ finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=signals");
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/project" }});
        await hooks["tool.execute.after"]({{
          sessionId: "session-1", tool: "edit", input: {{ filePath: "src/app.py" }}
        }});
        await hooks["session.idle"]({{ sessionId: "session-1" }});
        console.log(JSON.stringify(payloads));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    tool, idle = json.loads(result.stdout)
    assert tool["changed"] is True
    assert tool["dirty"] is True
    assert tool["significant"] is True
    assert idle["dirty"] is True
    for payload in (tool, idle):
        assert "token_percent" not in payload
        assert "compaction_confirmed" not in payload


def test_codex_project_state_observes_session_start_before_recovery(monkeypatch, tmp_path):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import codex_memory

    calls = []
    monkeypatch.setattr(
        codex_memory,
        "_observe_checkpoint_fail_open",
        lambda envelope: calls.append(("observe", envelope.event_type)),
    )
    monkeypatch.setattr(
        codex_memory,
        "_run_script",
        lambda *args: calls.append(("recover", args[0]))
        or subprocess.CompletedProcess([], 0, '{"hookSpecificOutput":{"additionalContext":""}}', ""),
    )
    monkeypatch.setattr(codex_memory, "_state_path", lambda path: ("demo", tmp_path / "state.md"))
    args = type("Args", (), {"cwd": str(tmp_path), "json": True})()

    assert codex_memory.command_project_state(args) == 0
    assert calls == [("observe", "session_start"), ("recover", "session_start_project_state.py")]


def test_opencode_vault_guard_uses_resolved_path_boundary():
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = "/work/wiki";
        const commands = [];
        globalThis.Bun = {{ spawn(args) {{
          commands.push(args);
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            exited: Promise.resolve(0),
            kill() {{}},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=vault");
        const sibling = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/work/wiki-client" }});
        const vault = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/work/wiki" }});
        await sibling["session.created"]({{}});
        await vault["session.created"]({{}});
        console.log(commands.length);
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_codex_wrapper_generates_context_file():
    """The Codex wrapper must generate cache/session-context.md before
    starting codex, so the agent has knowledge context available.
    """
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    assert "session_start_context" in wrapper, (
        "Codex wrapper must call session_start_context.py before codex starts"
    )
    assert "session-context.md" in wrapper, (
        "Codex wrapper must write to cache/session-context.md"
    )


def test_cursor_rules_has_mandatory_context_read():
    """Cursor rules file must instruct the agent to read the session
    context file at session start (MANDATORY).
    """
    rules = (ROOT / "integrations" / "cursor" / "rules" / "llm-wiki.mdc").read_text(encoding="utf-8")
    assert "session-context.md" in rules, (
        "Cursor rules must reference cache/session-context.md"
    )
    assert "MANDATORY" in rules.upper() or "first" in rules.lower(), (
        "Cursor rules must mark context reading as mandatory/first step"
    )


def test_antigravity_agents_has_mandatory_context_read():
    """Antigravity AGENTS.md must instruct the agent to read the session
    context file at session start (MANDATORY).
    """
    agents = (ROOT / "integrations" / "antigravity" / "AGENTS.md").read_text(encoding="utf-8")
    assert "session-context.md" in agents, (
        "Antigravity AGENTS.md must reference cache/session-context.md"
    )
    assert "MANDATORY" in agents.upper() or "first" in agents.lower(), (
        "Antigravity AGENTS.md must mark context reading as mandatory/first step"
    )


def test_session_start_context_supports_output_file():
    """session_start_context.py must support --output-file flag for
    writing context to a file (used by non-Claude agents).
    """
    script = (ROOT / "scripts" / "session_start_context.py").read_text(encoding="utf-8")
    assert "--output-file" in script, (
        "session_start_context.py must support --output-file flag"
    )
    assert "write_text" in script, (
        "session_start_context.py must write context to the output file"
    )


def test_install_scripts_generate_context(tmp_path):
    """Install scripts must generate the initial context file so the
    first session after install has knowledge context available.
    """
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "session_start_context" in install_ps1, (
        "install.ps1 must call session_start_context.py during OpenCode setup"
    )
    assert "session_start_context" in install_sh, (
        "install.sh must call session_start_context.py during OpenCode setup"
    )
    locked_mcp_sync = "uv sync --locked --extra mcp-server --quiet"
    assert locked_mcp_sync in install_sh
    assert locked_mcp_sync in install_ps1

    sh_codex = install_sh.split("# Codex CLI", 1)[1].split("# Cursor", 1)[0]
    sh_claude = install_sh.split("# Claude Code", 1)[1].split("# v4.0: OpenCode", 1)[0]
    sh_opencode = install_sh.split("# v4.0: OpenCode", 1)[1].split("# ─── 9.", 1)[0]
    ps_codex = install_ps1.split("# Codex", 1)[1].split("# Claude Code", 1)[0]
    ps_claude = install_ps1.split("# Claude Code", 1)[1].split("# Cursor", 1)[0]
    ps_opencode = install_ps1.split("# OpenCode", 1)[1].split("# Codex", 1)[0]

    assert 'CLAUDE_MCP="$HOME/.claude.json"' in sh_claude
    assert '"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--directory"' in sh_claude
    assert ".claude/.mcp.json" not in install_sh
    assert "Existing ~/.claude.json found without llm-wiki" in sh_claude
    assert "grep -q '\"llm-wiki\"'" in sh_claude

    assert 'OPENCODE_CONFIG="$HOME/.config/opencode/opencode.json"' in sh_opencode
    assert '"mcp":{"llm-wiki":{"type":"local","command":["uv","run","--directory"' in sh_opencode
    assert '"enabled":true' in sh_opencode
    assert '"mcpServers"' not in sh_opencode
    assert "Existing opencode.json found without llm-wiki" in sh_opencode
    assert "grep -q '\"llm-wiki\"'" in sh_opencode

    def parse_shell_json(block: str, destination: str) -> dict:
        line = next(line for line in block.splitlines() if f'> "${destination}"' in line)
        match = re.search(
            r"printf '%s\\n' '(.*?)'\"\$VAULT_JSON\"'(.*?)' >", line
        )
        assert match, line
        return json.loads(match.group(1) + '"ROOT"' + match.group(2))

    claude_json = parse_shell_json(sh_claude, "CLAUDE_MCP")
    assert claude_json == {
        "mcpServers": {
            "llm-wiki": {
                "command": "uv",
                "args": [
                    "run", "--directory", "ROOT", "python", "scripts/mcp_server.py"
                ],
            }
        }
    }
    opencode_json = parse_shell_json(sh_opencode, "OPENCODE_CONFIG")
    assert opencode_json == {
        "mcp": {
            "llm-wiki": {
                "type": "local",
                "command": [
                    "uv", "run", "--directory", "ROOT", "python",
                    "scripts/mcp_server.py",
                ],
                "enabled": True,
            }
        }
    }

    sh_codex_mcp = _shell_function(install_sh, "configure_codex_mcp")
    assert 'CODEX_CONFIG="$HOME/.codex/config.toml"' in sh_codex
    assert "config-state" in sh_codex_mcp
    assert "[mcp_servers.llm-wiki]" in sh_codex_mcp
    assert 'command = "uv"' in sh_codex_mcp
    assert 'args = [\\"run\\", \\"--directory\\"' in sh_codex_mcp
    assert "config.bak" in sh_codex_mcp
    assert "grep" not in sh_codex_mcp
    assert "install_codex_hooks" in sh_codex
    assert "merge-hooks" in install_sh

    assert '$claudeUserConfig = Join-Path $env:USERPROFILE ".claude.json"' in install_ps1
    assert "$claudeMcp = $claudeUserConfig" in ps_claude
    assert 'mcpServers = [ordered]@{' in ps_claude
    assert ".claude\\.mcp.json" not in install_ps1
    assert "Existing ~/.claude.json found without llm-wiki" in ps_claude
    assert "-notmatch '\"llm-wiki\"\\s*:'" in ps_claude

    assert '$openCodeMcp = Join-Path $openCodeConfig "opencode.json"' in ps_opencode
    assert 'mcp = [ordered]@{' in ps_opencode
    assert 'type = "local"' in ps_opencode
    assert 'command = @("uv", "run", "--directory", $VAULT_ROOT, "python", "scripts/mcp_server.py")' in ps_opencode
    assert "enabled = $true" in ps_opencode
    assert "mcpServers" not in ps_opencode
    assert "-notmatch '\"llm-wiki\"\\s*:'" in ps_opencode

    assert '$codexConfig = Join-Path $env:USERPROFILE ".codex\\config.toml"' in ps_codex
    assert "function Install-CodexMcp" in install_ps1
    assert "config-state" in install_ps1
    assert "[mcp_servers.llm-wiki]" in install_ps1
    assert 'command = "uv"' in install_ps1
    assert 'args = ["run", "--directory"' in install_ps1
    assert "Copy-Item -LiteralPath $Config" in install_ps1
    assert "Config.bak" in install_ps1
    assert "codex-memory-wrapper" in ps_codex
    assert "Install-CodexHooks" in ps_codex
    assert "merge-hooks" in install_ps1

    assert "v4.0 optional features" not in install_sh
    assert "mcp-server" not in install_sh.split("Useful commands:", 1)[-1]

    assert "function Write-Utf8NoBom" in install_ps1
    assert "[System.IO.File]::WriteAllText" in install_ps1
    assert "[System.Text.UTF8Encoding]::new($false)" in install_ps1
    for block, config_var in (
        (ps_opencode, "$openCodeMcp"),
        (ps_claude, "$claudeMcp"),
    ):
        assert f"Write-Utf8NoBom {config_var}" in block
        assert f"Set-Content -LiteralPath {config_var}" not in block
        assert f"Add-Content -LiteralPath {config_var}" not in block
    assert "Set-Content -LiteralPath $Config" not in install_ps1
    assert "Add-Content -LiteralPath $Config" not in install_ps1
    install_path = ROOT / "install.ps1"
    external = str(tmp_path / "external runtime")
    vault = str(tmp_path / "vault")
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(install_path))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Resolve-StateRoot'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Resolve-StateRoot missing' }}
        Invoke-Expression $fn.Extent.Text
        $first = Resolve-StateRoot -ProcessState '{external.replace("'", "''")}' -UserState '' -VaultRoot '{vault.replace("'", "''")}'
        $second = Resolve-StateRoot -ProcessState $first -UserState '' -VaultRoot '{vault.replace("'", "''")}'
        $fromUser = Resolve-StateRoot -ProcessState '' -UserState '{external.replace("'", "''")}' -VaultRoot '{vault.replace("'", "''")}'
        @($first, $second, $fromUser) | ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [external, external, external]
