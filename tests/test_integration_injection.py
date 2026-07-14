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
import re
import subprocess
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    assert "--checkpoint-type stop" in stop_command
    assert "--checkpoint-type significant_failure" in failure_command


def test_codex_wrapper_recovers_project_state_before_launch():
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    recovery = wrapper.index("codex_memory.py project-state")
    launch = wrapper.index("& $REAL_CODEX @fwdArgs")
    assert recovery < launch


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

    assert 'CODEX_CONFIG="$HOME/.codex/config.toml"' in sh_codex
    assert "[mcp_servers.llm-wiki]" in sh_codex
    assert 'command = "uv"' in sh_codex
    assert 'args = [\\"run\\", \\"--directory\\"' in sh_codex
    assert "CODEX_CONFIG.bak" in sh_codex
    assert "codex_memory.py daily-log" in sh_codex
    sh_args_line = next(line for line in sh_codex.splitlines() if "args = [" in line)
    sh_args = sh_args_line.split("args = ", 1)[1].rsplit('"', 1)[0]
    sh_args = sh_args.replace('\\"', '"').replace("$VAULT_JSON", '"ROOT"')
    assert json.loads(sh_args) == [
        "run", "--directory", "ROOT", "python", "scripts/mcp_server.py"
    ]

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
    assert "[mcp_servers.llm-wiki]" in ps_codex
    assert 'command = "uv"' in ps_codex
    assert 'args = ["run", "--directory"' in ps_codex
    assert "Copy-Item -LiteralPath $codexConfig" in ps_codex
    assert "codexConfig.bak" in ps_codex
    assert "codex-memory-wrapper" in ps_codex
    ps_args_line = next(line for line in ps_codex.splitlines() if line.startswith("args = ["))
    ps_args = ps_args_line.split("=", 1)[1].strip().replace('"$tomlVault"', '"ROOT"')
    assert json.loads(ps_args) == [
        "run", "--directory", "ROOT", "python", "scripts/mcp_server.py"
    ]

    assert "v4.0 optional features" not in install_sh
    assert "mcp-server" not in install_sh.split("Useful commands:", 1)[-1]

    assert "function Write-Utf8NoBom" in install_ps1
    assert "[System.IO.File]::WriteAllText" in install_ps1
    assert "[System.Text.UTF8Encoding]::new($false)" in install_ps1
    for block, config_var in (
        (ps_opencode, "$openCodeMcp"),
        (ps_claude, "$claudeMcp"),
        (ps_codex, "$codexConfig"),
    ):
        assert f"Write-Utf8NoBom {config_var}" in block
        assert f"Set-Content -LiteralPath {config_var}" not in block
        assert f"Add-Content -LiteralPath {config_var}" not in block
    assert "Copy-Item -LiteralPath $codexConfig" in ps_codex
    assert "$codexExisting +" in ps_codex
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
