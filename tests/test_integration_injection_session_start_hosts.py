"""Session start in OpenCode, Claude and Codex: the bounded project handoff, writer contention, and the hook output contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import integration_adapter
import memory_state
import pytest
from llm_wiki import (
    event_project,
    integration_capture_records,
    integration_delegates,
    integration_session_start,
)

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.integration_injection_case_helpers import (
    _existing_transcript,
    _unmet_substrings,
    _without_writer_wait,
    require_tool,
)
from tests.integration_injection_case_helpers import opencode_plugin_url as opencode_plugin_url
from tests.integration_injection_helpers import (
    ROOT,
    _ensure_scripts_on_path,
)
from tests.slow_machine import SHORT_TIMEOUT

# Session start must answer from the projection instead of recomputing the
# handoff or waiting for the Markdown writer gate. A four-core hosted Windows
# runner needed seven seconds for that projected read, so a sub-second ceiling
# measured the runner. The proof that survives load is the contrast with the
# writer hold below: blocking on the gate would cost WRITER_HOLD_SECONDS.
SESSION_START_BUDGET_SECONDS = 5.0


WRITER_HOLD_SECONDS = 30.0


def test_opencode_session_start_appends_recovered_bounded_project_handoff(monkeypatch):

    _ensure_scripts_on_path()
    import project_checkpoint
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

    monkeypatch.setattr(project_checkpoint, "observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        event_project, "project_context", lambda event: ("demo", Path("C:/project"))
    )
    monkeypatch.setattr(integration_session_start, "record_activity", lambda *args: True)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda args: None)
    monkeypatch.setattr(
        integration_session_start,
        "build_session_start_context",
        lambda slug=None: "# General memory\n",
    )
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
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
    monkeypatch, tmp_path, opencode_plugin_url: str
):

    _ensure_scripts_on_path()
    import project_checkpoint
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
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(project_checkpoint, "observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(integration_session_start, "record_activity", lambda *args: True)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda args: None)
    monkeypatch.setattr(integration_session_start, "build_session_start_context", lambda slug=None: "")
    envelope = integration_adapter.normalize_event(
        "opencode",
        "session_start",
        {"sessionId": "unicode-session", "directory": str(project)},
    )

    started = time.perf_counter()
    result = integration_adapter.ingest_event(envelope)
    elapsed = time.perf_counter() - started
    shared = recover_project_handoff(ProjectStore(vault, state_root), slug, project_root=project)

    assert (
        elapsed < SESSION_START_BUDGET_SECONDS,
        result["context"],
        len(result["context"]) <= 2400,
        (project_state / "journal.md").exists(),
    ) == (
        True,
        shared.context,
        True,
        False,
    )
    assert (
        _unmet_substrings(
            (
                ("Preserve older handoff", result["context"], True),
                ("Preserve older open thread", result["context"], True),
                (f"project:{slug}", result["context"], True),
            )
        )
        == []
    )
    plugin_url = opencode_plugin_url
    script = textwrap.dedent(
        f"""
        globalThis.Bun = {{ spawn() {{
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            stdout: new Blob([{json.dumps(json.dumps({"context": result["context"]}))}]).stream(),
            exited: Promise.resolve(0),
            kill() {{}},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)});
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: {json.dumps(str(project))} }});
        await hooks.event({{ event: {{
          type: "session.created",
          properties: {{ sessionID: "unicode-session", info: {{ id: "unicode-session" }} }},
        }} }});
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
        [
            require_tool("node"),
            "--input-type=module",
            "--eval",
            script,
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=180,
    )
    assert node.returncode == 0, node.stderr
    assert json.loads(node.stdout) == [result["context"]]


def test_opencode_session_start_project_recovery_is_fail_open(monkeypatch):

    _ensure_scripts_on_path()
    import project_checkpoint

    class Store:
        def __init__(self, *args):
            raise RuntimeError("recovery unavailable")

    errors = []
    monkeypatch.setattr(project_checkpoint, "observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        event_project, "project_context", lambda event: ("demo", Path("C:/project"))
    )
    monkeypatch.setattr(integration_session_start, "record_activity", lambda *args: True)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda args: None)
    monkeypatch.setattr(
        integration_session_start,
        "build_session_start_context",
        lambda slug=None: "# General memory\n",
    )
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(
        project_checkpoint, "log_checkpoint_error", lambda error: errors.append(str(error))
    )
    envelope = integration_adapter.normalize_event(
        "opencode", "session_start", {"directory": "C:/project"}
    )

    result = integration_adapter.ingest_event(envelope)

    assert result["context"] == "# General memory\n"
    assert errors == ["recovery unavailable"]


def test_opencode_session_start_writer_contention_is_bounded_and_degraded(monkeypatch, tmp_path):

    _ensure_scripts_on_path()
    import project_checkpoint
    from project_journal import ProjectStore

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project_dir = tmp_path / "project"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    project_dir.mkdir()
    store = ProjectStore(vault, state_root)
    held = threading.Event()

    release = threading.Event()

    def hold_writer():
        with store.coordinator.writer_gate():
            held.set()
            release.wait(WRITER_HOLD_SECONDS)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(project_checkpoint, "observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        event_project, "project_context", lambda event: ("demo", project_dir)
    )
    monkeypatch.setattr(integration_session_start, "record_activity", lambda *args: True)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda args: None)
    monkeypatch.setattr(
        integration_session_start, "build_session_start_context", lambda slug=None: "# General memory\n"
    )
    envelope = integration_adapter.normalize_event(
        "opencode", "session_start", {"directory": str(project_dir)}
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_writer)
        assert held.wait(SHORT_TIMEOUT)
        started = time.perf_counter()
        result = integration_adapter.ingest_event(envelope)
        elapsed = time.perf_counter() - started
        release.set()
        holder.result(timeout=WRITER_HOLD_SECONDS)

    assert elapsed < SESSION_START_BUDGET_SECONDS
    assert (
        _unmet_substrings(
            (
                ("# General memory", result["context"], True),
                ("Degraded", result["context"], True),
                ("project:demo", result["context"], True),
                ("recovery:project:demo", result["context"], True),
            )
        )
        == []
    )


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_claude_and_codex_project_state_are_bounded_under_writer_contention(host, tmp_path):
    import os

    _ensure_scripts_on_path()
    import project_checkpoint
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
            "delta": project_checkpoint.empty_delta(),
            "evidence_event_ids": ["test"],
        },
        "test",
    )
    held = threading.Event()
    release_writer = threading.Event()

    def hold_writer():
        with store.coordinator.writer_gate():
            held.set()
            assert release_writer.wait(timeout=SHORT_TIMEOUT)

    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(vault)
    env["LLM_WIKI_STATE_ROOT"] = str(state_root)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    command = _project_state_command(host, vault, project_dir)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_writer)
        assert held.wait(SHORT_TIMEOUT)
        try:
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
        finally:
            release_writer.set()
        holder.result(timeout=SHORT_TIMEOUT)

    assert result.returncode == 0, result.stderr
    context = _project_state_context(host, json.loads(result.stdout))
    unmet = _unmet_substrings(
        (
            ("project:demo", context, True),
            ("sequence:1", context, True),
            ("Degraded", context, True),
            ("recovery:project:demo", context, True),
        )
    )
    assert (len(context) <= 2400, unmet) == (True, [])


def _project_state_command(host: str, vault: Path, project_dir: Path) -> list[str]:
    """The host's project-state entry point; Codex runs it from a vault copy."""
    if host != "codex":
        return [sys.executable, str(ROOT / "scripts/session_start_project_state.py")]
    shutil.copytree(ROOT / "scripts", vault / "scripts")
    return [
        sys.executable,
        str(ROOT / "scripts/codex_memory.py"),
        "project-state",
        "--cwd",
        str(project_dir),
        "--json",
    ]


def _project_state_context(host: str, payload: dict) -> str:
    if host == "claude":
        return payload["hookSpecificOutput"]["additionalContext"]
    return payload["additional_context"]


def test_claude_hooks_cover_compaction_failure_stop_and_end_signals():
    settings = json.loads(
        (ROOT / "integrations" / "claude-code" / "settings.json").read_text(encoding="utf-8")
    )
    hooks = settings["hooks"]
    assert {
        "SessionStart",
        "PreCompact",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SessionEnd",
    } <= set(hooks)
    stop_command = hooks["Stop"][0]["hooks"][0]["command"]
    failure_command = hooks["PostToolUseFailure"][0]["hooks"][0]["command"]
    assert (
        _unmet_substrings(
            (
                ("--event stop", stop_command, True),
                ("--event session_end", stop_command, False),
                ("--delegate", stop_command, False),
                ("--checkpoint-type significant_failure", failure_command, True),
            )
        )
        == []
    )


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


def test_claude_outer_session_start_preserves_hook_output_contract(monkeypatch, capsys):
    import io
    import sys


    monkeypatch.setattr(
        integration_adapter,
        "ingest_event",
        lambda _envelope, **_options: {"context": "combined context\n"},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert integration_adapter.main(["--source", "claude", "--event", "session_start"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "combined context\n",
        }
    }


def test_claude_session_end_uses_one_adapter_occurrence_for_both_side_effects(
    monkeypatch, tmp_path
):
    import io
    import sys

    _ensure_scripts_on_path()
    import project_checkpoint

    settings = json.loads(
        (ROOT / "integrations/claude-code/settings.json").read_text(encoding="utf-8")
    )
    hooks = settings["hooks"]["SessionEnd"][0]["hooks"]
    assert len(hooks) == 1
    assert (
        _unmet_substrings(
            (
                ("--event session_end", hooks[0]["command"], True),
                ("--delegate", hooks[0]["command"], False),
            )
        )
        == []
    )

    calls = []

    def observe(envelope):
        calls.append(("observe", envelope.event_id))

    def delegate(name, _payload, **_kwargs):
        calls.append(("delegate", name))
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    def capture(envelope, *_args):
        calls.append(("capture", envelope.event_id))
        return "1" * 64

    def wake(args):
        calls.append(("wake", args))
        return 123

    monkeypatch.setattr(
        project_checkpoint,
        "observe_project_checkpoint",
        _without_writer_wait(observe),
    )
    monkeypatch.setattr(integration_delegates, "run_delegate", delegate)
    monkeypatch.setattr(
        integration_capture_records,
        "publish_durable_capture_intent",
        capture,
    )
    monkeypatch.setattr(memory_state, "spawn_detached", wake)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "s1",
                    "cwd": "C:/project",
                    "transcript_path": _existing_transcript(tmp_path),
                }
            )
        ),
    )

    assert (
        integration_adapter.main(["--source", "claude", "--event", "session_end"]),
        calls[0][0],
        calls[1],
        calls[2],
        calls[3][0],
    ) == (
        0,
        "observe",
        ("capture", calls[0][1]),
        ("delegate", "session_end_project_tag.py"),
        "wake",
    )


def test_codex_wrapper_recovers_project_state_before_launch():
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    recovery = wrapper.index("codex_memory.py project-state")
    launch = wrapper.index("& $REAL_CODEX @fwdArgs")
    assert recovery < launch
