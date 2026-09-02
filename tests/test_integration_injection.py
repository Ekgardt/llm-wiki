"""Guard tests for thin native lifecycle integrations.

These tests ensure that:
1. The OpenCode plugin forwards lifecycle events to shared Python ingestion.
2. Native integrations do not duplicate MCP reads or classification logic.
3. The Codex wrapper generates a context file before codex starts.
4. session_start_context.py supports --output-file mode
5. The install scripts generate the initial context file

If any of these are removed, CI catches it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
# Session start must answer from the projection instead of recomputing the
# handoff or waiting for the Markdown writer gate. A four-core hosted Windows
# runner needed seven seconds for that projected read, so a sub-second ceiling
# measured the runner. The proof that survives load is the contrast with the
# writer hold below: blocking on the gate would cost WRITER_HOLD_SECONDS.
SESSION_START_BUDGET_SECONDS = 5.0
WRITER_HOLD_SECONDS = 30.0


def _existing_transcript(tmp_path) -> str:
    """A transcript the adapter can actually find.

    The fixture used to name `C:/tmp/session.jsonl`, which exists on no machine
    this suite runs on, so these tests only ever exercised the branch by having
    the key present. Since 2026-09-02 a path that names nothing is treated as no
    transcript — which is what production does when a session leaves no file —
    so the fixture has to be a real one.
    """
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    return str(transcript)


def require_tool(name: str) -> str:
    """Resolve an optional external runtime or skip the calling test.

    Node and PowerShell 7 are optional for contributors (README: "Node 22 is
    optional"), so a missing binary must skip rather than fail the suite.
    """
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is unavailable")
    return path


def _ensure_scripts_on_path() -> None:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def _hook_handlers(merged: dict) -> list[dict]:
    return [
        handler
        for groups in merged["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]


def _codex_memory_handlers(merged: dict) -> list[dict]:
    handlers = _hook_handlers(merged)
    return [handler for handler in handlers if "codex_memory.py" in handler.get("command", "")]


def _stop_hook_commands(merged: dict) -> list[str]:
    groups = merged["hooks"]["Stop"]
    return [handler.get("command") for group in groups for handler in group["hooks"]]


@pytest.fixture
def opencode_plugin_url(tmp_path: Path) -> str:
    plugin = tmp_path / "llm-wiki-memory-opencode.mjs"
    shutil.copyfile(ROOT / "scripts" / "llm-wiki-memory-opencode.js", plugin)
    return plugin.resolve().as_uri()


def test_opencode_plugin_is_lifecycle_only():
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert "integration_adapter.py" in plugin
    assert "event: async" in plugin
    assert '"session.created"' in plugin
    assert '"tool.execute.after"' in plugin
    assert '"session.idle"' in plugin
    assert '"experimental.session.compacting"' in plugin
    assert '"session.created": async' not in plugin
    assert '"session.idle": async' not in plugin
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


def test_opencode_roleless_user_message_is_forwarded_once(opencode_plugin_url: str):
    plugin_url = opencode_plugin_url
    root = "D:/vault"
    directory = "D:/project"
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        const calls = [];
        globalThis.Bun = {{ spawn(args) {{
          const record = {{ args, stdin: "" }};
          calls.push(record);
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{
              write(value) {{ record.stdin += value; }},
              end() {{ finish(0); }},
            }},
            stdout: new ReadableStream({{ start(controller) {{ controller.close(); }} }}),
            exited,
            kill() {{ finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import(
          {json.dumps(plugin_url)} + "?harness=roleless-user-message"
        );
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: {json.dumps(directory)} }});
        const roleless = [
          {{ sessionID: "session-1" }},
          {{
            message: {{ id: "message-1" }},
            parts: [
              {{ type: "text", text: "Preserve this request" }},
              {{ type: "file", text: "ignore this attachment text" }},
              {{ type: "text", text: "and this second part" }},
            ],
          }},
        ];
        await hooks["chat.message"](...roleless);
        await hooks["chat.message"](
          {{ sessionID: "session-1" }},
          {{
            message: {{ id: "message-2", role: "assistant" }},
            parts: [{{ type: "text", text: "do not capture" }}],
          }},
        );
        console.log(JSON.stringify(calls));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)
    assert len(calls) == 1
    assert calls[0]["args"][-4:] == ["--source", "opencode", "--event", "user_prompt"]
    assert json.loads(calls[0]["stdin"]) == {
        "directory": directory,
        "event_id": "message-1",
        "prompt": "Preserve this request\nand this second part",
        "sessionID": "session-1",
    }


def test_installed_plugin_captures_without_an_inherited_environment(tmp_path: Path):
    """OpenCode from a desktop launcher has no `LLM_WIKI_ROOT`; the copy carries it."""
    import sys as _sys

    scripts = ROOT / "scripts"
    if str(scripts) not in _sys.path:
        _sys.path.insert(0, str(scripts))
    from installer_config import plugin_with_embedded_root

    root = tmp_path / "vault"
    published = tmp_path / "installed-plugin.mjs"
    source = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    published.write_text(plugin_with_embedded_root(source, root), encoding="utf-8")
    script = textwrap.dedent(
        f"""
        delete process.env.LLM_WIKI_ROOT;
        const calls = [];
        globalThis.Bun = {{ spawn(args) {{
          const record = {{ args, stdin: "" }};
          calls.push(record);
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{
              write(value) {{ record.stdin += value; }},
              end() {{ finish(0); }},
            }},
            stdout: new ReadableStream({{ start(controller) {{ controller.close(); }} }}),
            exited,
            kill() {{ finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(published.resolve().as_uri())});
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: {json.dumps(str(tmp_path / "project"))} }});
        await hooks["chat.message"](
          {{ sessionID: "session-1" }},
          {{ message: {{ id: "message-1" }}, parts: [{{ type: "text", text: "capture me" }}] }},
        );
        console.log(JSON.stringify(calls));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)
    assert len(calls) == 1
    # The plugin joins with a forward slash, which Windows accepts in a path.
    assert f"{root}/scripts" in " ".join(calls[0]["args"])


def test_user_prompt_ingestion_runs_prompt_and_feedback_capture_once(monkeypatch):
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter

    calls = []
    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        integration_adapter,
        "_project_context",
        lambda event: ("demo", Path("D:/project")),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: calls.append((name, payload, kwargs)),
    )
    envelope = integration_adapter.normalize_event(
        "opencode",
        "user_prompt",
        {
            "sessionID": "session-1",
            "directory": "D:/project",
            "event_id": "message-1",
            "prompt": "Preserve this request",
        },
    )

    integration_adapter.ingest_event(envelope)

    assert [name for name, _, _ in calls] == [
        "user_prompt_capture.py",
        "feedback_capture.py",
    ]
    assert calls[0][1]["prompt"] == "Preserve this request"
    assert calls[1][1] == {
        "text": "Preserve this request",
        "session_id": "session-1",
        "slug": "demo",
        "trigger": "opencode-user-message",
    }


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


def test_adapter_observes_same_envelope_once_before_durable_capture(
    monkeypatch, tmp_path
):
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
        "_publish_durable_capture_intent",
        lambda envelope, *_args: calls.append(("capture", envelope)) or "1" * 64,
    )
    monkeypatch.setattr(integration_adapter, "_run_delegate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda _args: 123)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "s1",
                    "cwd": "C:/project",
                    "reason": "done",
                    "transcript_path": _existing_transcript(tmp_path),
                }
            )
        ),
    )

    assert (
        integration_adapter.main(
            ["--source", "claude", "--event", "session_end", "--delegate", "session_end_capture.py"]
        )
        == 0
    )
    assert [name for name, _ in calls] == ["observe", "capture"]
    assert calls[1][1] is calls[0][1]


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
    # A checkpoint is named after the batch it commits, not after one of its
    # members — see `_batch_occurrence_id`. Each of these batches holds one
    # event, and the two names must still differ.
    assert [checkpoint["occurrence_id"] for checkpoint in checkpoints] == [
        integration_adapter._batch_occurrence_id([first.event_id]),
        integration_adapter._batch_occurrence_id([second.event_id]),
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
    assert checkpoints[0]["occurrence_id"] == integration_adapter._batch_occurrence_id(
        [envelope.event_id]
    )


def test_durable_capture_runs_when_checkpoint_observation_fails(
    monkeypatch, capsys, tmp_path
):
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
        "_publish_durable_capture_intent",
        lambda *_args: calls.append("capture") or "1" * 64,
    )
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda _args: 123)
    monkeypatch.setattr(
        integration_adapter,
        "_log_checkpoint_error",
        lambda error: calls.append(("logged", len(str(error)))),
        raising=False,
    )
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
        integration_adapter.main(
            ["--source", "codex", "--event", "session_end", "--delegate", "session_end_capture.py"]
        )
        == 0
    )
    assert calls == [("logged", 2000), "capture", "session_end_project_tag.py"]
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


def _session_end_events(adapter, project_dir: Path, count: int) -> list:
    return [
        adapter.normalize_event(
            "codex",
            "session_end",
            {
                "session_id": "session-1",
                "cwd": str(project_dir),
                "event_id": f"event-{index}",
            },
        )
        for index in range(count)
    ]


def _journal_records(journal: str, header: str) -> list[dict]:
    lines = journal.removeprefix(header).splitlines()
    return [json.loads(line) for line in lines if line]


def test_concurrent_distinct_events_are_each_journaled_exactly_once(monkeypatch, tmp_path):
    _ensure_scripts_on_path()
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
    events = _session_end_events(integration_adapter, project_dir, 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(integration_adapter._observe_project_checkpoint, events))

    journal = ProjectStore(vault, state_root).read_journal("demo")
    records = _journal_records(journal, JOURNAL_HEADER)
    assert sorted(record["occurrence_id"] for record in records) == sorted(
        integration_adapter._batch_occurrence_id([event.event_id]) for event in events
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
    # The retry of a batch must reuse the batch's name; that is what makes the
    # coordinator collapse it instead of refusing it.
    assert attempts == [
        integration_adapter._batch_occurrence_id([first.event_id]),
        integration_adapter._batch_occurrence_id([first.event_id]),
        integration_adapter._batch_occurrence_id([second.event_id]),
    ]
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
    assert checkpoints == [
        integration_adapter._batch_occurrence_id([event.event_id]),
        integration_adapter._batch_occurrence_id([event.event_id]),
    ]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_session_start_maintenance_does_not_debounce_or_drop_following_delta(monkeypatch):
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import integration_adapter
    from project_journal import CheckpointReducer

    previous = integration_adapter.datetime.fromisoformat("2026-07-13T11:59:29+00:00")
    session_start_at = integration_adapter.datetime.fromisoformat("2026-07-13T12:00:00+00:00")
    ordinary_event_at = integration_adapter.datetime.fromisoformat("2026-07-13T12:00:01+00:00")
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
    assert checkpoints[0]["occurrence_id"] == integration_adapter._batch_occurrence_id(
        [ordinary.event_id]
    )
    assert checkpoints[0]["delta"]["current_task"] == delta["current_task"]
    assert checkpoints[0]["delta"]["current_task_operations"] == [delta["current_task"]]


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
        delta(current_task={"id": "task-1", "action": "upsert", "value": "First"}),
    )
    blocker = event(
        "blocker-1",
        2,
        "blocker_opened",
        delta(blockers=[{"id": "blocker-1", "action": "upsert", "value": "Waiting"}]),
    )
    file_change = event(
        "file-1",
        3,
        "file_changed",
        delta(
            current_task={"id": "task-1", "action": "upsert", "value": "Latest"},
            blockers=[{"id": "blocker-1", "action": "close", "value": "Resolved"}],
            changed_files=[{"id": "src/app.py", "action": "upsert", "value": "src/app.py"}],
        ),
    )
    timer = event("timer-1", 31)

    for envelope in (correction, blocker, file_change):
        integration_adapter._observe_project_checkpoint(envelope)
    assert checkpoints == []
    assert [item["event_id"] for item in state["project_checkpoint_pending"]["demo"]] == [
        correction.event_id,
        blocker.event_id,
        file_change.event_id,
    ]

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


def _pending_delta(adapter, index: int) -> dict:
    delta = adapter._empty_delta()
    delta["current_task"] = {
        "id": f"task-{index}",
        "action": "upsert",
        "value": f"Task {index}",
    }
    delta["decisions"] = [
        {"id": f"decision-{index}", "action": "upsert", "value": f"Decision {index}"}
    ]
    return delta


def _batch_checkpoint_type(index: int, last: int) -> str:
    if index == last:
        return "decision"
    return "correction"


def _batch_offset_seconds(index: int, last: int) -> int:
    if index == last:
        return 2
    return 1


def _seed_pending_checkpoints(adapter, state: dict, started, total: int) -> list[str]:
    last = total - 1
    event_ids: list[str] = []
    for index in range(total):
        envelope = adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "session_id": "s1",
                "cwd": "C:/project",
                "event_id": f"event-{index}",
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "checkpoint_type": _batch_checkpoint_type(index, last),
                "project_delta": _pending_delta(adapter, index),
            },
            occurred_at=started + timedelta(seconds=_batch_offset_seconds(index, last)),
        )
        event_ids.append(envelope.event_id)
        state["project_checkpoint_pending"]["demo"].append(
            adapter._pending_checkpoint(envelope, "demo", "demo:s1")
        )
    return event_ids


def _evidence_lengths(checkpoints: list[dict]) -> list[int]:
    return [len(item["evidence_event_ids"]) for item in checkpoints]


def _task_operation_lengths(checkpoints: list[dict]) -> list[int]:
    return [len(item["delta"]["current_task_operations"]) for item in checkpoints]


def _decision_lengths(checkpoints: list[dict]) -> list[int]:
    return [len(item["delta"]["decisions"]) for item in checkpoints]


def _flat_evidence_ids(checkpoints: list[dict]) -> list[str]:
    return [event_id for item in checkpoints for event_id in item["evidence_event_ids"]]


def test_bypass_flush_batches_205_pending_events_across_failure_and_restart(monkeypatch):
    _ensure_scripts_on_path()
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
    event_ids = _seed_pending_checkpoints(integration_adapter, state, started, 205)

    with pytest.raises(RuntimeError, match="second batch interrupted"):
        integration_adapter._drain_project_checkpoints("demo", "demo")
    assert len(checkpoints) == 1
    assert len(checkpoints[0]["evidence_event_ids"]) == 100
    assert len(state["project_checkpoint_pending"]["demo"]) == 105

    state = json.loads(json.dumps(state))
    integration_adapter._drain_project_checkpoints("demo", "demo")

    assert _evidence_lengths(checkpoints) == [100, 100, 5]
    assert _task_operation_lengths(checkpoints) == [100, 100, 5]
    assert max(_decision_lengths(checkpoints)) <= 100
    assert _flat_evidence_ids(checkpoints) == event_ids
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
        {"id": f"decision-{index}", "action": "upsert", "value": str(index)} for index in range(205)
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
    monkeypatch, tmp_path, opencode_plugin_url: str
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
    shared = recover_project_handoff(ProjectStore(vault, state_root), slug, project_root=project)

    assert elapsed < SESSION_START_BUDGET_SECONDS
    assert result["context"] == shared.context
    assert len(result["context"]) <= 2400
    assert "Preserve older handoff" in result["context"]
    assert "Preserve older open thread" in result["context"]
    assert f"project:{slug}" in result["context"]
    assert not (project_state / "journal.md").exists()

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


def test_opencode_session_start_writer_contention_is_bounded_and_degraded(monkeypatch, tmp_path):
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

    release = threading.Event()

    def hold_writer():
        with store.coordinator.writer_gate():
            held.set()
            release.wait(WRITER_HOLD_SECONDS)

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
        release.set()
        holder.result(timeout=WRITER_HOLD_SECONDS)

    assert elapsed < SESSION_START_BUDGET_SECONDS
    assert "# General memory" in result["context"]
    assert "Degraded" in result["context"]
    assert "project:demo" in result["context"]
    assert "recovery:project:demo" in result["context"]


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_claude_and_codex_project_state_are_bounded_under_writer_contention(host, tmp_path):
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
    release_writer = threading.Event()

    def hold_writer():
        with store.coordinator.writer_gate():
            held.set()
            assert release_writer.wait(timeout=10)

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
        holder.result(timeout=5)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    if host == "claude":
        context = payload["hookSpecificOutput"]["additionalContext"]
    else:
        context = payload["additional_context"]
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


def test_claude_outer_session_start_preserves_hook_output_contract(monkeypatch, capsys):
    import io
    import sys

    import integration_adapter

    monkeypatch.setattr(
        integration_adapter,
        "ingest_event",
        lambda _envelope: {"context": "combined context\n"},
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
        integration_adapter,
        "_observe_project_checkpoint",
        observe,
    )
    monkeypatch.setattr(integration_adapter, "_run_delegate", delegate)
    monkeypatch.setattr(
        integration_adapter,
        "_publish_durable_capture_intent",
        capture,
    )
    monkeypatch.setattr(integration_adapter, "spawn_detached", wake)
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

    assert integration_adapter.main(["--source", "claude", "--event", "session_end"]) == 0
    assert calls[0][0] == "observe"
    assert calls[1] == ("capture", calls[0][1])
    assert calls[2] == ("delegate", "session_end_project_tag.py")
    assert calls[3][0] == "wake"


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
    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    destination = tmp_path / "hooks.json"
    original = (
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
        )
        + "\r\n"
    )
    destination.write_bytes(original.encode("utf-8"))

    codex_memory.merge_codex_hooks(source, destination)
    first = destination.read_bytes()
    backups = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original.encode("utf-8")
    codex_memory.merge_codex_hooks(source, destination)
    merged = json.loads(destination.read_text(encoding="utf-8"))

    assert destination.read_bytes() == first
    assert list(tmp_path.glob("hooks.json.bak-llm-wiki-*")) == backups
    assert merged["custom"] == {"preserved": True}
    assert "echo user" in _stop_hook_commands(merged)
    assert len(_codex_memory_handlers(merged)) == 4


def _owned_backup_mtime(now: float, index: int) -> float:
    if index == 0:
        return now - 91 * 24 * 60 * 60
    return now - (11 - index)


def _seed_owned_backups(tmp_path: Path, now: float, count: int) -> Path:
    for index in range(count):
        backup = tmp_path / f"hooks.json.bak-llm-wiki-20260801-000000-{index:06d}"
        backup.write_bytes(str(index).encode("ascii"))
        modified = _owned_backup_mtime(now, index)
        os.utime(backup, (modified, modified))
    return tmp_path / "hooks.json.bak-llm-wiki-20260801-000000-000000"


def test_codex_hook_merge_prunes_owned_backups_but_keeps_newest_and_unrelated(tmp_path):
    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    destination = tmp_path / "hooks.json"
    original = b'{"hooks":{}}\n'
    destination.write_bytes(original)
    now = time.time()
    old = _seed_owned_backups(tmp_path, now, 11)
    unrelated = tmp_path / "hooks.json.backup"
    unrelated.write_bytes(b"keep")

    codex_memory.merge_codex_hooks(source, destination)

    backups = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert not old.exists()
    assert len(backups) <= 10
    assert original in [path.read_bytes() for path in backups]
    assert unrelated.read_bytes() == b"keep"


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
            blocks.append(f"matcher = {json.dumps(group['matcher'])}")
        handler = group["hooks"][0]
        blocks.extend(
            [
                "",
                f"[[hooks.{event_name}.hooks]]",
                'type = "command"',
                f"command = {json.dumps(handler['command'])}",
                f"command_windows = {json.dumps(handler['commandWindows'])}",
                f"timeout = {handler['timeout']}",
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


def test_codex_hook_command_reports_equivalent_inline_without_creating_json(tmp_path, capsys):
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
    installed["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "echo user"}]})
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
def test_codex_hooks_feature_state_obeys_canonical_precedence(tmp_path, features, expected):
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
        f'args = ["run", "--locked", "--no-sync", "--directory", {json.dumps(str(ROOT))}, '
        '"python", "scripts/mcp_server.py"]\nenabled = true\n',
        encoding="utf-8",
    )

    assert codex_memory.codex_mcp_config_state(config, ROOT) == "equivalent"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('model = "gpt-5.6"\n', "absent"),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["python", "other.py"]\n',
            "conflict",
        ),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
            'args = ["run", "--locked", "--no-sync", "--directory", "wrong", "python", '
            '"scripts/mcp_server.py"]\nenabled = false\n',
            "conflict",
        ),
    ],
)
def test_codex_mcp_config_state_distinguishes_absent_and_conflicting(tmp_path, body, expected):
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
        assert "hooks-state" in installer
        assert "--config" in installer
        assert "/hooks" in installer
        assert "trust" in installer.casefold()
    assert "not installed automatically" in powershell.casefold()


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", source)
    assert match, f"{name} missing"
    return match.group(0)


def _installer_test_section(source: str) -> str:
    section = source.split("4. Run production smoke", 1)[1].split(
        "5. Set environment variables", 1
    )[0]
    return section.split("\n", 1)[1]


def _write_fake_uv(directory: Path, *, exit_code: int, last_line: str) -> None:
    fake_uv = directory / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ \"$i\" -lt 100 ]; do echo 'filler output'; i=$((i + 1)); done\n"
        f"printf '%s\\n' {json.dumps(last_line)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)


@pytest.fixture(scope="module")
def windows_fake_uv(tmp_path_factory):
    if os.name != "nt":
        return None
    compiler = shutil.which("powershell")
    if compiler is None:
        pytest.skip("Windows PowerShell compiler unavailable")
    build = tmp_path_factory.mktemp("native-fake-uv")
    source = build / "fake-uv.cs"
    executable = build / "uv.exe"
    source.write_text(
        "using System;\n"
        "using System.Diagnostics;\n"
        "using System.IO;\n"
        "using System.Threading;\n"
        "public static class FakeUv {\n"
        "  public static int Main() {\n"
        '    var pidFile = Environment.GetEnvironmentVariable("FAKE_UV_PID_FILE");\n'
        '    var startedFile = Environment.GetEnvironmentVariable("FAKE_UV_STARTED_FILE");\n'
        "    if (!String.IsNullOrEmpty(pidFile)) File.WriteAllText(pidFile, Process.GetCurrentProcess().Id.ToString());\n"
        '    if (!String.IsNullOrEmpty(startedFile)) File.WriteAllText(startedFile, "started");\n'
        '    var output = Environment.GetEnvironmentVariable("FAKE_UV_OUTPUT");\n'
        "    if (!String.IsNullOrEmpty(output)) Console.WriteLine(output);\n"
        '    if (Environment.GetEnvironmentVariable("FAKE_UV_MODE") == "block") {\n'
        '      var python = Environment.GetEnvironmentVariable("FAKE_UV_PYTHON");\n'
        '      var childScript = Environment.GetEnvironmentVariable("FAKE_UV_CHILD_SCRIPT");\n'
        '      var childInfo = new ProcessStartInfo(python, "\\"" + childScript + "\\"");\n'
        "      childInfo.UseShellExecute = false;\n"
        "      Process.Start(childInfo);\n"
        "      Thread.Sleep(Timeout.Infinite);\n"
        "    }\n"
        "    int code;\n"
        '    return Int32.TryParse(Environment.GetEnvironmentVariable("FAKE_UV_EXIT"), out code) ? code : 0;\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    try:
        compiled = subprocess.run(
            [
                compiler,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Add-Type -Path {json.dumps(str(source))} "
                f"-OutputAssembly {json.dumps(str(executable))} "
                "-OutputType ConsoleApplication",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("Windows PowerShell compiler unavailable within 120 seconds")
    if compiled.returncode != 0:
        pytest.skip(f"Windows PowerShell compiler unavailable: {compiled.stderr[-500:]}")
    return executable


# The deadline the installer is given, and how long the fake `uv` would run if
# nothing stopped it. The gap between the two is what makes a surviving child
# visible at all: a run that lasts the sleep instead of the deadline was never
# killed, so both numbers belong in the failure rather than in two literals.
_SMOKE_TIMEOUT_SECONDS = 15
_FAKE_UV_SLEEP_SECONDS = 60


def _child_state(directory: Path) -> str:
    """What the stopped child left behind, for a failure that must explain itself."""
    markers = ("child.started", "child.pid", "child.stopped", "child.completed")
    seen = {
        name: (directory / name).read_text(encoding="utf-8") or "yes"
        for name in markers
        if (directory / name).exists()
    }
    return f"child markers: {seen or 'none'}"


def _write_blocking_fake_uv(directory: Path) -> None:
    fake_uv = directory / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        # The trap comes first, before anything that can block or be
        # descheduled. Everything between exec and this line is a window in
        # which the installer's timer kills the child before it can record
        # having been stopped, and the test then fails for the machine being
        # busy rather than for the installer being wrong.
        "stop_child() { : > child.stopped; exit 0; }\n"
        "trap stop_child HUP INT TERM\n"
        "printf '%s' \"$$\" > child.pid\n"
        ": > child.started\n"
        # `wait` is interruptible by a trap; a foreground command is not. With
        # the sleep in the foreground the shell reached its handler only after
        # the sleep returned, which spent part of the installer's half-second
        # escalation window on nothing. Backgrounding it costs the test nothing
        # and hands the whole window to the handler.
        # Long enough that the installer's own timer, not the sleep, ends it,
        # and short enough that a missed signal still ends the run with the
        # assertion that names the cause instead of the caller's wall clock.
        f"sleep {_FAKE_UV_SLEEP_SECONDS} &\n"
        "wait\n"
        ": > child.completed\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)


def _write_stubborn_fake_uv_tree(directory: Path) -> None:
    grandchild = directory / "fake-pytest-grandchild"
    grandchild.write_text(
        "#!/bin/bash\n"
        "printf '%s' \"$$\" > grandchild.pid\n"
        ": > grandchild.started\n"
        "trap '' HUP INT TERM\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    grandchild.chmod(0o700)
    child = directory / "fake-pytest-child"
    child.write_text(
        "#!/bin/bash\n"
        "printf '%s' \"$$\" > child.pid\n"
        "trap '' HUP INT TERM\n"
        '"$(dirname "$0")/fake-pytest-grandchild" &\n'
        "wait\n",
        encoding="utf-8",
    )
    child.chmod(0o700)
    fake_uv = directory / "uv"
    fake_uv.write_text(
        "#!/bin/bash\n"
        "printf '%s' \"$$\" > uv.pid\n"
        "stop_uv() { exit 0; }\n"
        "trap stop_uv HUP INT TERM\n"
        '"$(dirname "$0")/fake-pytest-child" &\n'
        "wait\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)


def _bash_search_paths() -> list[str | None]:
    git = shutil.which("git")
    if git is None:
        return [None]
    return [None, str(Path(git).resolve().parent.parent / "bin")]


def _bash_runs(bash: str) -> bool:
    try:
        result = subprocess.run(
            [bash, "--version"], capture_output=True, timeout=5, check=False
        )
    except OSError:
        return False
    return result.returncode == 0


def _working_bash_at(search_path: str | None) -> str | None:
    bash = shutil.which("bash", path=search_path)
    if bash is None or not _bash_runs(bash):
        return None
    return bash


def _find_working_bash() -> str | None:
    if os.name == "nt":
        return None
    candidates = (_working_bash_at(path) for path in _bash_search_paths())
    return next((bash for bash in candidates if bash is not None), None)


@pytest.mark.parametrize(
    ("fake_exit", "fake_output", "expected_exit", "expected_marker"),
    [
        (1, "smoke ok", 1, "[FAIL] Production smoke failed; installation aborted"),
        (0, "smoke failed", 0, "[OK] Production smoke passed"),
    ],
    ids=["smoke_exit_1_success_output", "smoke_exit_0_failure_output"],
)
def test_unix_installer_trusts_smoke_exit_status(
    tmp_path, fake_exit, fake_output, expected_exit, expected_marker
):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin, exit_code=fake_exit, last_line=fake_output)
    runner = tmp_path / "installer-pytest-contract.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            RED='' GREEN='' YELLOW='' BLUE='' NC=''
            PATH="$(dirname "$0")/bin:$PATH"
            export PATH
            info() {{ echo "[INFO] $1"; }}
            ok() {{ echo "[OK] $1"; }}
            warn() {{ echo "[WARN] $1"; }}
            fail() {{ echo "[FAIL] $1"; exit 1; }}
            {section}
            case "$-" in *m*) exit 91 ;; esac
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS"] = "5"

    result = subprocess.run(
        [bash, str(runner)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    assert expected_marker in result.stdout
    assert fake_output in result.stdout


def test_unix_installer_timeout_stops_tests_and_aborts(tmp_path):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_blocking_fake_uv(fake_bin)
    runner = tmp_path / "installer-timeout-contract.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            # The timer must outlast the child reaching its signal trap. The
            # fake `uv` now installs that trap as its first statement, so the
            # window is one fork and exec; one second raced it and five raced it
            # again while the trap sat behind two writes.
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS={_SMOKE_TIMEOUT_SECONDS}
            PATH="$(dirname "$0")/bin:$PATH"
            export PATH
            info() {{ :; }}
            ok() {{ : > passed.marker; }}
            warn() {{ :; }}
            fail() {{ printf '%s' "$1" > failed.message; exit 1; }}
            {section}
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    runner.chmod(0o700)

    started = time.monotonic()
    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stderr
    assert (tmp_path / "failed.message").read_text() == (
        f"Production smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s; installation aborted"
    )
    # The child must be gone because the installer killed it, not because it
    # ran out of work of its own. Those two look identical in the marker alone,
    # and the clock tells them apart: a run that lasts as long as the fake `uv`
    # sleeps was never signalled, while a run that ends at the deadline was.
    # The measured failure of this assertion was the second kind — the handler
    # reached its escalation 45s late — and `assert not True` said none of it.
    assert not (tmp_path / "child.completed").exists(), (
        f"the smoke child ran to completion instead of being killed: "
        f"installer took {elapsed:.1f}s for a {_SMOKE_TIMEOUT_SECONDS}s deadline "
        f"against a {_FAKE_UV_SLEEP_SECONDS}s child; {_child_state(tmp_path)}; "
        f"stderr={result.stderr[-400:]!r}"
    )
    assert not (tmp_path / "passed.marker").exists()
    assert not (tmp_path / "continued.marker").exists()
    # Named last and with its evidence: this is the assertion that fails when a
    # loaded machine keeps the child off the CPU for the whole half-second the
    # installer waits before escalating to KILL. Without the evidence the
    # failure reads `assert False` and says nothing about which happened.
    assert (tmp_path / "child.stopped").exists(), _child_state(tmp_path)


@pytest.mark.parametrize(
    ("signal_name", "expected_exit"),
    [("HUP", 129), ("INT", 130), ("TERM", 143)],
)
def test_unix_installer_signal_traps_cleanup_and_exit(tmp_path, signal_name, expected_exit):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_blocking_fake_uv(fake_bin)
    continued = tmp_path / "continued.marker"
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=30
            PATH="$(dirname "$0")/bin:$PATH"
            export PATH
            info() {{ :; }}
            ok() {{ :; }}
            warn() {{ :; }}
            {section}
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-signal.sh"
    orchestrator.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            # A shell without job control starts an asynchronous job with
            # SIGINT ignored, and an ignored signal cannot be trapped. bash 5
            # installs the trap here anyway, bash 3.2 on macOS does not, so
            # the installer never saw the INT it traps. Job control gives the
            # job its own process group with the default disposition, which is
            # how a terminal delivers Ctrl-C to a real installation.
            set -m
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f child.started ]; then started=1; break; fi
              if ! kill -0 "$installerPid" 2>/dev/null; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s {signal_name} "$installerPid"
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            childPid="$(cat child.pid)"
            if kill -0 "$childPid" 2>/dev/null; then
              : > child.alive
              kill -TERM "$childPid"
            fi
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "installer.status").read_text() == str(expected_exit)
    assert (tmp_path / "child.stopped").exists()
    assert not (tmp_path / "child.completed").exists()
    assert not (tmp_path / "child.alive").exists()
    assert not continued.exists()


def test_unix_installer_signal_kills_complete_stubborn_test_tree(tmp_path):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stubborn_fake_uv_tree(fake_bin)
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=30
            PATH="$(dirname "$0")/bin:$PATH"
            export PATH
            info() {{ :; }}
            ok() {{ :; }}
            warn() {{ :; }}
            {section}
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-tree-signal.sh"
    orchestrator.write_text(
        textwrap.dedent(
            """
            set -euo pipefail
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f grandchild.started ]; then started=1; break; fi
              if ! kill -0 "$installerPid" 2>/dev/null; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s TERM "$installerPid"
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            : > survivors
            for pidFile in uv.pid child.pid grandchild.pid; do
              pid="$(cat "$pidFile")"
              if kill -0 "$pid" 2>/dev/null; then
                printf '%s:%s\n' "$pidFile" "$pid" >> survivors
                kill -s KILL "$pid" 2>/dev/null || :
              fi
            done
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "installer.status").read_text() == "143"
    assert (tmp_path / "survivors").read_text() == ""
    assert not (tmp_path / "continued.marker").exists()


def test_unix_installer_initial_monitor_mode_cleans_stopped_test_tree(tmp_path):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stubborn_fake_uv_tree(fake_bin)
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set -m
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=3
            PATH="$(dirname "$0")/bin:$PATH"
            export PATH
            info() {{ :; }}
            ok() {{ : > passed.marker; }}
            warn() {{ : > warned.marker; }}
            fail() {{
              case "$-" in *m*) : > monitor-restored.marker ;; esac
              : > failed.marker
              exit 1
            }}
            {section}
            case "$-" in *m*) : > monitor-restored.marker ;; esac
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-stopped-tree.sh"
    orchestrator.write_text(
        textwrap.dedent(
            """
            set -euo pipefail
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f grandchild.started ]; then started=1; break; fi
              if ! kill -0 "$installerPid" 2>/dev/null; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s STOP "$(cat uv.pid)"
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            : > survivors
            for pidFile in uv.pid child.pid grandchild.pid; do
              pid="$(cat "$pidFile")"
              if kill -0 "$pid" 2>/dev/null; then
                printf '%s:%s\n' "$pidFile" "$pid" >> survivors
                kill -s KILL "$pid" 2>/dev/null || :
              fi
            done
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "installer.status").read_text() == "1"
    assert (tmp_path / "survivors").read_text() == ""
    assert (tmp_path / "failed.marker").exists()
    assert not (tmp_path / "passed.marker").exists()
    assert (tmp_path / "monitor-restored.marker").exists()
    assert not (tmp_path / "continued.marker").exists()


@pytest.mark.parametrize("stop_signal", ["STOP", "TTIN"])
def test_unix_installer_initial_monitor_off_cleans_stopped_test_tree(tmp_path, stop_signal):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    section = _installer_test_section((ROOT / "install.sh").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stubborn_fake_uv_tree(fake_bin)
    installer = tmp_path / "installer-under-test.sh"
    installer.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set +m
            export LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS=3
            PATH="$(dirname "$0")/bin:$PATH"
            export PATH
            info() {{ :; }}
            ok() {{ : > passed.marker; }}
            warn() {{ : > warned.marker; }}
            fail() {{
              case "$-" in
                *m*) : > monitor-wrong.marker ;;
                *) : > monitor-restored.marker ;;
              esac
              : > failed.marker
              exit 1
            }}
            {section}
            case "$-" in
              *m*) : > monitor-wrong.marker ;;
              *) : > monitor-restored.marker ;;
            esac
            : > continued.marker
            """
        ),
        encoding="utf-8",
    )
    installer.chmod(0o700)
    orchestrator = tmp_path / "orchestrate-stopped-tree.sh"
    orchestrator.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            ./installer-under-test.sh &
            installerPid=$!
            started=0
            for ((attempt = 0; attempt < 500; attempt++)); do
              if [ -f grandchild.started ]; then started=1; break; fi
              if ! kill -0 "$installerPid" 2>/dev/null; then break; fi
              sleep 0.01
            done
            if [ "$started" -ne 1 ]; then exit 90; fi
            kill -s {stop_signal} "$(cat uv.pid)"
            finished=0
            # Bash reports a stopped job to `wait` only where job control does
            # it; elsewhere the installer's own smoke timer ends the wait, so
            # the poll has to outlast that timer.
            for ((attempt = 0; attempt < 1000; attempt++)); do
              if ! kill -0 "$installerPid" 2>/dev/null; then finished=1; break; fi
              sleep 0.01
            done
            if [ "$finished" -ne 1 ]; then
              : > hung.marker
              kill -s TERM "$installerPid" 2>/dev/null || :
            fi
            if wait "$installerPid"; then
              installerExit=0
            else
              installerExit=$?
            fi
            printf '%s' "$installerExit" > installer.status
            : > survivors
            for pidFile in uv.pid child.pid grandchild.pid; do
              pid="$(cat "$pidFile")"
              if kill -0 "$pid" 2>/dev/null; then
                printf '%s:%s\n' "$pidFile" "$pid" >> survivors
                kill -s KILL "$pid" 2>/dev/null || :
              fi
            done
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(orchestrator)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "hung.marker").exists()
    assert (tmp_path / "installer.status").read_text() == "1"
    assert (tmp_path / "survivors").read_text() == ""
    assert (tmp_path / "failed.marker").exists()
    assert not (tmp_path / "passed.marker").exists()
    assert (tmp_path / "monitor-restored.marker").exists()
    assert not (tmp_path / "monitor-wrong.marker").exists()
    assert not (tmp_path / "continued.marker").exists()
    assert result.stdout == ""
    assert result.stderr == ""


def test_unix_installer_sigttin_wait_status_enters_bounded_group_cleanup(tmp_path):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _shell_function(source, name)
        for name in (
            "restore_test_monitor_mode",
            "test_tree_alive",
            "stop_test_child",
            "stop_test_timer",
            "wait_test_child",
        )
    )
    runner = tmp_path / "exercise-stopped-wait.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set -m
            calls="$(pwd)/calls"
            : > "$calls"
            waitCount=0
            killed=0
            kill() {{
              printf 'kill %s\n' "$*" >> "$calls"
              case "$*" in
                "-0 -- -4242") [ "$killed" -eq 0 ] ;;
                "-s KILL -- -4242") killed=1; return 0 ;;
                *) return 0 ;;
              esac
            }}
            sleep() {{ printf 'sleep %s\n' "$*" >> "$calls"; }}
            wait() {{
              waitCount=$((waitCount + 1))
              printf 'wait %s\n' "$*" >> "$calls"
              [ "$waitCount" -gt 1 ] && return 143
              return 149
            }}
            testPid=4242
            testPgid=4242
            testTimerPid=""
            testMonitorMode=on
            {functions}
            if wait_test_child; then status=0; else status=$?; fi
            printf '%s' "$status" > status
            case "$-" in *m*) : > monitor-restored ;; esac
            printf '%s:%s' "$testPid" "$testPgid" > state
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (tmp_path / "status").read_text() == "149"
    assert (tmp_path / "state").read_text() == ":"
    assert (tmp_path / "monitor-restored").exists()
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls.count("wait 4242") == 2
    assert "kill -s TERM -- -4242" in calls
    assert "kill -s CONT -- -4242" in calls
    assert "kill -s KILL -- -4242" in calls


def test_unix_installer_signal_trap_restores_initial_monitor_mode(tmp_path):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _shell_function(source, name)
        for name in (
            "restore_test_monitor_mode",
            "test_tree_alive",
            "stop_test_child",
            "stop_test_timer",
            "handle_test_signal",
        )
    )
    runner = tmp_path / "exercise-signal-mode-restore.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            set -m
            testPid=""
            testPgid=""
            testTimerPid=""
            testMonitorMode=off
            exitStatus=""
            exit() {{ exitStatus="$1"; }}
            {functions}
            handle_test_signal 143
            printf '%s' "$exitStatus" > status
            case "$-" in
              *m*) : > monitor-wrong ;;
              *) : > monitor-restored ;;
            esac
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (tmp_path / "status").read_text() == "143"
    assert (tmp_path / "monitor-restored").exists()
    assert not (tmp_path / "monitor-wrong").exists()


def test_unix_installer_cleanup_targets_group_with_term_then_kill(tmp_path):
    bash = _find_working_bash()
    if bash is None:
        pytest.skip("bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _shell_function(source, name) for name in ("test_tree_alive", "stop_test_child")
    )
    runner = tmp_path / "exercise-cleanup.sh"
    runner.write_text(
        textwrap.dedent(
            f"""
            set -euo pipefail
            calls="$(pwd)/calls"
            : > "$calls"
            kill() {{
              printf 'kill %s\n' "$*" >> "$calls"
              return 0
            }}
            sleep() {{ printf 'sleep %s\n' "$*" >> "$calls"; }}
            wait() {{ printf 'wait %s\n' "$*" >> "$calls"; return 0; }}
            testPid=4242
            testPgid=4242
            {functions}
            stop_test_child
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(runner)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert "kill -s TERM -- -4242" in calls
    assert "kill -s KILL -- -4242" in calls
    assert calls.index("kill -s TERM -- -4242") < calls.index("kill -s KILL -- -4242")
    assert calls.count("sleep 0.1") == 5
    assert calls[-1] == "wait 4242"


@pytest.mark.parametrize(
    ("fake_exit", "fake_output", "expected_exit", "expected_marker"),
    [
        (1, "smoke ok", 1, "[FAIL] Production smoke failed; installation aborted"),
        (0, "smoke failed", 0, "[OK] Production smoke passed"),
    ],
    ids=["smoke_exit_1_success_output", "smoke_exit_0_failure_output"],
)
@pytest.mark.parametrize("powershell_name", ["powershell", "pwsh"])
def test_windows_installer_trusts_smoke_exit_status(
    tmp_path,
    windows_fake_uv,
    powershell_name,
    fake_exit,
    fake_output,
    expected_exit,
    expected_marker,
):
    powershell = shutil.which(powershell_name)
    if powershell is None:
        pytest.skip(f"{powershell_name} unavailable")
    section = _installer_test_section((ROOT / "install.ps1").read_text(encoding="utf-8"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin, exit_code=fake_exit, last_line=fake_output)
    if windows_fake_uv is not None:
        shutil.copy2(windows_fake_uv, fake_bin / "uv.exe")
    command = textwrap.dedent(
        f"""
        $ErrorActionPreference = "Stop"
        if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {{
            $PSNativeCommandUseErrorActionPreference = $true
        }}
        function Info($msg) {{ Write-Output "[INFO] $msg" }}
        function Ok($msg) {{ Write-Output "[OK] $msg" }}
        function Warn($msg) {{ Write-Output "[WARN] $msg" }}
        function Fail($msg) {{ Write-Output "[FAIL] $msg"; exit 1 }}
        {section}
        """
    )
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        FAKE_UV_EXIT=str(fake_exit),
        FAKE_UV_OUTPUT=fake_output,
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr
    assert expected_marker in result.stdout
    assert fake_output in result.stdout


def _windows_process_survived(pid_file: Path) -> bool:
    import ctypes

    pid = int(pid_file.read_text())
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    assert ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    alive = exit_code.value == 259
    if alive:
        ctypes.windll.kernel32.TerminateProcess(handle, 1)
    ctypes.windll.kernel32.CloseHandle(handle)
    return alive


def _windows_survivors(targets) -> list[str]:
    return [
        f"{label}:{pid_file.read_text()}"
        for label, pid_file in targets
        if _windows_process_survived(pid_file)
    ]


@pytest.mark.parametrize("powershell_name", ["powershell", "pwsh"])
def test_windows_installer_error_stops_native_child_and_later_steps(
    tmp_path, windows_fake_uv, powershell_name
):
    if os.name != "nt":
        pytest.skip("Windows-only native process cleanup contract")
    powershell = shutil.which(powershell_name)
    if powershell is None:
        pytest.skip(f"{powershell_name} unavailable")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shutil.copy2(windows_fake_uv, fake_bin / "uv.exe")
    started = tmp_path / "child.started"
    uv_pid_file = tmp_path / "uv.pid"
    python_pid_file = tmp_path / "python.pid"
    python_started = tmp_path / "python.started"
    child_script = tmp_path / "blocking-child.py"
    child_script.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_PYTHON_PID_FILE']).write_text(str(os.getpid()))\n"
        "Path(os.environ['FAKE_PYTHON_STARTED_FILE']).write_text('started')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    later = tmp_path / "later.marker"
    section = _installer_test_section((ROOT / "install.ps1").read_text(encoding="utf-8"))
    injected_wait = textwrap.dedent(
        """
        $testDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not (Test-Path -LiteralPath $env:FAKE_PYTHON_STARTED_FILE)) {
            if ([DateTime]::UtcNow -ge $testDeadline) { throw "fake uv did not start" }
            Start-Sleep -Milliseconds 10
        }
        throw "injected installer interruption"
        """
    ).strip()
    section = section.replace(
        "if (-not $testProcess.WaitForExit($testTimeoutMilliseconds)) {",
        injected_wait + "\nif ($false) {",
        1,
    )
    command = textwrap.dedent(
        f"""
        $ErrorActionPreference = "Stop"
        function Info($msg) {{ Write-Output "[INFO] $msg" }}
        function Ok($msg) {{ Write-Output "[OK] $msg" }}
        function Warn($msg) {{ Write-Output "[WARN] $msg" }}
        {section}
        New-Item -ItemType File -Path {json.dumps(str(later))} | Out-Null
        """
    )
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        FAKE_UV_MODE="block",
        FAKE_UV_PID_FILE=str(uv_pid_file),
        FAKE_UV_STARTED_FILE=str(started),
        FAKE_UV_PYTHON=sys.executable,
        FAKE_UV_CHILD_SCRIPT=str(child_script),
        FAKE_PYTHON_PID_FILE=str(python_pid_file),
        FAKE_PYTHON_STARTED_FILE=str(python_started),
    )
    error_log = tmp_path / "powershell-error.log"
    with error_log.open("wb") as stderr:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            timeout=180,
            check=False,
        )

    assert result.returncode != 0
    assert b"injected installer interruption" in error_log.read_bytes()
    assert started.exists()
    assert python_started.exists()
    assert not later.exists()

    survivors = _windows_survivors((("uv", uv_pid_file), ("python", python_pid_file)))
    assert not survivors, f"native process tree survived installer cleanup: {survivors}"


def _codex_mcp_toml(vault: Path, *, quoted: bool, conflicting: bool = False) -> str:
    table = '[mcp_servers."llm-wiki"]' if quoted else "[mcp_servers.llm-wiki]"
    args = (
        ["python", "other.py"]
        if conflicting
        else [
            "run",
            "--locked",
            "--no-sync",
            "--directory",
            str(vault),
            "python",
            "scripts/mcp_server.py",
        ]
    )
    return f'{table}\ncommand = "uv"\nargs = {json.dumps(args)}\n'


@pytest.mark.parametrize(
    ("scenario", "expected_exit"),
    [("equivalent", 0), ("conflicting", 2), ("absent", 0)],
    ids=["quoted-equivalent", "conflicting", "absent"],
)
def test_unix_installer_mcp_function_uses_parser_in_temp_home(tmp_path, scenario, expected_exit):
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
        else _codex_mcp_toml(ROOT, quoted=True, conflicting=scenario == "conflicting")
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
def test_windows_installer_mcp_function_uses_parser_in_temp_home(tmp_path, scenario, expected_exit):
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = (
        'model = "gpt-5.6"\n'
        if scenario == "absent"
        else _codex_mcp_toml(ROOT, quoted=True, conflicting=scenario == "conflicting")
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
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        $code = Install-CodexMcp -VaultRoot {json.dumps(str(ROOT))} -Config {json.dumps(str(config))}
        exit $code
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
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


def test_unix_installer_probe_reports_absent_without_touching_the_file(tmp_path):
    """The probe replaced a merge: it must answer and change nothing.

    The installer asks this before the ownership transaction and owns
    `hooks.json` only when the answer is `absent`.
    """
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _shell_function(source, "codex_inline_hooks_state")
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    destination = codex_dir / "hooks.json"
    destination.write_text(
        '{"custom":true,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    before = destination.read_bytes()
    runner = tmp_path / "installer-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != hooks-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\n"
        + 'codex_inline_hooks_state "$TEST_VAULT" "$HOME/.codex"\n',
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

    assert result.stdout.strip() == "absent"
    assert destination.read_bytes() == before

def test_unix_installer_probe_reports_conflict_for_unrelated_inline_hooks(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    function = _shell_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "codex_inline_hooks_state"
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
        + "  while [[ $# -gt 0 && $1 != hooks-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'codex_inline_hooks_state "$TEST_VAULT" "$HOME/.codex"\n',
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

    assert result.stdout.strip() == "conflict"
    assert destination.read_bytes() == before

def test_windows_installer_probe_reports_absent_without_touching_the_file(tmp_path):
    """The probe replaced a merge: it must answer and change nothing."""
    source = ROOT / "install.ps1"
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    destination = codex_dir / "hooks.json"
    destination.write_text(
        '{"custom":true,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
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
            $node.Name -eq 'Get-CodexInlineHooksState'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Get-CodexInlineHooksState missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'hooks-state')
            if ($index -lt 0) {{ throw 'hooks-state missing' }}
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        Get-CodexInlineHooksState -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "absent"
    assert destination.read_bytes() == before

def test_windows_installer_probe_reports_conflict_for_partial_inline_hooks(tmp_path):
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
            $node.Name -eq 'Get-CodexInlineHooksState'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Get-CodexInlineHooksState missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'hooks-state')
            if ($index -lt 0) {{ throw 'hooks-state missing' }}
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        Get-CodexInlineHooksState -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "conflict"
    assert destination.read_bytes() == before

def test_unix_installer_probe_reports_disabled_when_the_feature_is_off(tmp_path):
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash unavailable")
    function = _shell_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "codex_inline_hooks_state"
    )
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text("[features]\ncodex_hooks = false\n", encoding="utf-8")
    destination = codex_dir / "hooks.json"
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()
    runner = tmp_path / "installer-disabled-contract.sh"
    runner.write_text(
        function
        + "\nuv() {\n"
        + "  while [[ $# -gt 0 && $1 != hooks-state ]]; do shift; done\n"
        + '  command "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n'
        + "}\nset +e\n"
        + 'codex_inline_hooks_state "$TEST_VAULT" "$HOME/.codex"\n',
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

    assert result.stdout.strip() == "disabled"
    assert destination.read_bytes() == before

def test_windows_installer_probe_reports_disabled_when_the_feature_is_off(tmp_path):
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
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Get-CodexInlineHooksState'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Get-CodexInlineHooksState missing' }}
        Invoke-Expression $fn.Extent.Text
        function uv {{
            $all = @($args)
            $index = [Array]::IndexOf($all, 'hooks-state')
            if ($index -lt 0) {{ throw 'hooks-state missing' }}
            & {json.dumps(sys.executable)} {json.dumps(str(ROOT / "scripts/codex_memory.py"))} $all[$index..($all.Count - 1)]
        }}
        Get-CodexInlineHooksState -VaultRoot {json.dumps(str(ROOT))} -CodexDir {json.dumps(str(codex_dir))}
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.stdout.strip() == "disabled"
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


def test_opencode_node_harness_forwards_bounded_tail_and_escaped_paths(
    tmp_path, opencode_plugin_url: str
):
    plugin_url = opencode_plugin_url
    root = str(tmp_path / "Vault With Spaces")
    directory = str(tmp_path / "Project With Spaces")
    adapter_context = json.dumps(
        {
            "context": (
                "# Project memory context\n\n## Health\n\nScheduler degraded.\n\n"
                "# Project handoff\n\n## MCP identifiers\n"
                "- `project:demo`\n- `sequence:7`\n"
            )
        }
    )
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
        await hooks.event({{ event: {{
          type: "session.idle", properties: {{ sessionID: "session-1" }},
        }} }});
        await hooks["experimental.session.compacting"]({{ sessionId: "session-1" }});
        await hooks.event({{ event: {{
          type: "session.created",
          properties: {{ sessionID: "session-1", info: {{ id: "session-1" }} }},
        }} }});
        const system = [];
        await hooks["experimental.chat.system.transform"](
          {{ sessionID: "session-1" }}, {{ system }}
        );
        console.log(JSON.stringify({{ commands, requests, system }}));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
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
        "--locked",
        "--no-sync",
        "--directory",
        root,
        "python",
        f"{root}/scripts/integration_adapter.py",
        "--source",
        "opencode",
        "--event",
        "session_end",
    ]


def test_opencode_node_harness_times_out_stalled_capture(tmp_path, opencode_plugin_url: str):
    plugin_url = opencode_plugin_url
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
        await hooks.event({{ event: {{
          type: "session.created", properties: {{ sessionID: "session-1" }},
        }} }});
        console.log(JSON.stringify({{ elapsed: Date.now() - started, killed }}));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["elapsed"] < 500
    assert observed["killed"] is True


def test_opencode_forwards_known_mutation_and_dirty_idle_without_fake_progress_signals(
    tmp_path, opencode_plugin_url: str
):
    plugin_url = opencode_plugin_url
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
        await hooks.event({{ event: {{
          type: "session.idle", properties: {{ sessionID: "session-1" }},
        }} }});
        console.log(JSON.stringify(payloads));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
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
        lambda *args: (
            calls.append(("recover", args[0]))
            or subprocess.CompletedProcess(
                [], 0, '{"hookSpecificOutput":{"additionalContext":""}}', ""
            )
        ),
    )
    monkeypatch.setattr(codex_memory, "_state_path", lambda path: ("demo", tmp_path / "state.md"))
    args = type("Args", (), {"cwd": str(tmp_path), "json": True})()

    assert codex_memory.command_project_state(args) == 0
    assert calls == [("observe", "session_start"), ("recover", "session_start_project_state.py")]


def test_opencode_vault_guard_uses_resolved_path_boundary(opencode_plugin_url: str):
    plugin_url = opencode_plugin_url
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
        const created = {{ event: {{
          type: "session.created", properties: {{ sessionID: "session-1" }},
        }} }};
        await sibling.event(created);
        await vault.event(created);
        // A number goes through `util.inspect`, which colours it when
        // FORCE_COLOR is set in the developer's shell; a string never does.
        console.log(String(commands.length));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
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
    assert "session-context.md" in wrapper, "Codex wrapper must write to cache/session-context.md"


def test_session_start_context_supports_output_file():
    """session_start_context.py must support --output-file flag for
    writing context to a file (used by non-Claude agents).
    """
    script = (ROOT / "scripts" / "session_start_context.py").read_text(encoding="utf-8")
    assert "--output-file" in script, "session_start_context.py must support --output-file flag"
    assert "write_text" in script, "session_start_context.py must write context to the output file"


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
    assert "sync-args" in install_sh
    assert "sync-args" in install_ps1
    assert "--locked" in install_sh
    assert "--no-default-groups" in install_sh
    assert "--locked" in install_ps1
    assert "--no-default-groups" in install_ps1

    sh_codex = install_sh.split("# Codex CLI", 1)[1].split("# Claude Code", 1)[0]
    sh_claude = install_sh.split("# Claude Code", 1)[1].split("# OpenCode configuration", 1)[0]
    sh_opencode = install_sh.split("# OpenCode configuration", 1)[1].split("# Codex CLI", 1)[0]
    ps_codex = install_ps1.split("# Codex", 1)[1].split("# Claude Code", 1)[0]
    ps_claude = install_ps1.split("# Claude Code", 1)[1].split("# --- 8.", 1)[0]
    ps_opencode = install_ps1.split("# OpenCode", 1)[1].split("# Codex", 1)[0]

    assert 'CLAUDE_MCP="$HOME/.claude.json"' in sh_claude
    assert (
        '"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--locked","--no-sync","--directory"'
        in sh_claude
    )
    assert ".claude/.mcp.json" not in install_sh
    assert "Existing ~/.claude.json found without llm-wiki" in sh_claude
    assert "grep -q '\"llm-wiki\"'" in sh_claude

    assert "scripts/installer_config.py" in sh_opencode
    assert "opencode" in sh_opencode
    assert '--root "$VAULT_ROOT"' in sh_opencode
    assert '--state-root "$STATE_ROOT"' in sh_opencode
    assert '--cwd "$CALLER_CWD"' in sh_opencode
    assert "active)" in sh_opencode
    assert "conflict)" in sh_opencode
    assert "configured_unverified)" in sh_opencode
    assert "not_detected)" in sh_opencode
    assert "OPENCODE_CONFIG=" not in sh_opencode
    assert "grep" not in sh_opencode

    def parse_shell_json(block: str, destination: str) -> dict:
        line = next(line for line in block.splitlines() if f'> "${destination}"' in line)
        match = re.search(r"printf '%s\\n' '(.*?)'\"\$VAULT_JSON\"'(.*?)' >", line)
        assert match, line
        return json.loads(match.group(1) + '"ROOT"' + match.group(2))

    claude_json = parse_shell_json(sh_claude, "CLAUDE_MCP")
    assert claude_json == {
        "mcpServers": {
            "llm-wiki": {
                "command": "uv",
                "args": [
                    "run",
                    "--locked",
                    "--no-sync",
                    "--directory",
                    "ROOT",
                    "python",
                    "scripts/mcp_server.py",
                ],
            }
        }
    }
    sh_codex_mcp = _shell_function(install_sh, "configure_codex_mcp")
    assert 'CODEX_CONFIG="$HOME/.codex/config.toml"' in sh_codex
    assert "config-state" in sh_codex_mcp
    assert "[mcp_servers.llm-wiki]" in sh_codex_mcp
    assert 'command = "uv"' in sh_codex_mcp
    assert 'args = [\\"run\\", \\"--locked\\", \\"--no-sync\\", \\"--directory\\"' in sh_codex_mcp
    assert "config.bak" in sh_codex_mcp
    assert "grep" not in sh_codex_mcp
    assert "$CODEX_HOOKS_STATE" in sh_codex
    assert "codex_inline_hooks_state" in install_sh
    assert "hooks-state" in install_sh

    assert '$claudeUserConfig = Join-Path $env:USERPROFILE ".claude.json"' in install_ps1
    assert "$claudeMcp = $claudeUserConfig" in ps_claude
    assert "mcpServers = [ordered]@{" in ps_claude
    assert ".claude\\.mcp.json" not in install_ps1
    assert "Existing ~/.claude.json found without llm-wiki" in ps_claude
    assert "-notmatch '\"llm-wiki\"\\s*:'" in ps_claude

    assert "scripts\\installer_config.py" in ps_opencode
    assert '"opencode", "--root", $VAULT_ROOT' in ps_opencode
    assert '"--state-root", $STATE_ROOT' in ps_opencode
    assert '"--cwd", $callerDirectory' in ps_opencode
    assert '"active" {' in ps_opencode
    assert '"conflict" {' in ps_opencode
    assert '"configured_unverified" {' in ps_opencode
    assert '"not_detected" {' in ps_opencode
    assert "$openCodeMcp" not in ps_opencode
    assert "-notmatch" not in ps_opencode

    assert '$codexConfig = Join-Path $env:USERPROFILE ".codex\\config.toml"' in ps_codex
    assert "function Install-CodexMcp" in install_ps1
    assert "config-state" in install_ps1
    assert "[mcp_servers.llm-wiki]" in install_ps1
    assert 'command = "uv"' in install_ps1
    assert 'args = ["run", "--locked", "--no-sync", "--directory"' in install_ps1
    assert "Copy-Item -LiteralPath $Config" in install_ps1
    assert "Config.bak" in install_ps1
    assert "codex-memory-wrapper" in ps_codex
    assert "$codexHooksState" in ps_codex
    assert "hooks-state" in install_ps1

    assert "v4.0 optional features" not in install_sh
    assert "mcp-server" not in install_sh.split("Useful commands:", 1)[-1]

    assert "function Write-Utf8NoBom" in install_ps1
    assert "[System.IO.File]::WriteAllText" in install_ps1
    assert "[System.Text.UTF8Encoding]::new($false)" in install_ps1
    for block, config_var in ((ps_claude, "$claudeMcp"),):
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
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        # pwsh startup alone can take ten seconds on a loaded hosted runner.
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [external, external, external]


def test_windows_scheduler_payload_carries_exact_roots_and_uv(tmp_path):
    script = ROOT / "scripts" / "install-scheduled-tasks.ps1"
    runner = ROOT / "scripts" / "run-scheduled-task.ps1"
    root = str(tmp_path / "vault's source")
    state = str(tmp_path / "state's data")
    uv_path = str(tmp_path / "bin's tools" / "uv.exe")

    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(script))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'New-LLMWikiScheduledAction'
        }}, $true)
        if ($null -eq $fn) {{ throw 'New-LLMWikiScheduledAction missing' }}
        Invoke-Expression $fn.Extent.Text
        function New-ScheduledTaskAction {{
            param($Execute, $Argument)
            [pscustomobject]@{{ Execute = $Execute; Argument = $Argument }}
        }}
        $action = New-LLMWikiScheduledAction `
            -Kind nightly `
            -VaultRoot {ps_literal(root)} `
            -StateRoot {ps_literal(state)} `
            -UvPath {ps_literal(uv_path)} `
            -RunnerPath {ps_literal(str(runner))} `
            -PowerShellPath {ps_literal(shutil.which("pwsh") or "pwsh")}
        $encoded = ($action.Argument -split '\\s+')[-1]
        $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
        [pscustomobject]@{{ Execute = $action.Execute; Decoded = $decoded }} |
            ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        # pwsh startup alone can take ten seconds on a loaded hosted runner.
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    decoded = value["Decoded"]
    quoted_root = root.replace("'", "''")
    quoted_state = state.replace("'", "''")
    quoted_uv = uv_path.replace("'", "''")
    assert Path(value["Execute"]).name.casefold() in {
        "pwsh",
        "pwsh.exe",
        "powershell.exe",
    }
    assert "-Kind 'nightly'" in decoded
    assert f"-VaultRoot '{quoted_root}'" in decoded
    assert f"-StateRoot '{quoted_state}'" in decoded
    assert f"-UvPath '{quoted_uv}'" in decoded
    assert str(runner).replace("'", "''") in decoded


def test_windows_scheduler_status_accepts_only_the_registered_contract(tmp_path):
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is unavailable")
    script = ROOT / "scripts" / "install-scheduled-tasks.ps1"
    root = str(tmp_path / "vault")
    state = str(tmp_path / "state")
    uv_path = str(tmp_path / "uv.exe")
    runner = str(tmp_path / "vault/scripts/run-scheduled-task.ps1")

    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(script))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        foreach ($name in @('New-LLMWikiScheduledAction', 'Test-LLMWikiScheduledTasks')) {{
            $fn = $ast.Find({{ param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $name
            }}, $true)
            if ($null -eq $fn) {{ throw "$name missing" }}
            Invoke-Expression $fn.Extent.Text
        }}
        function New-ScheduledTaskAction {{
            param($Execute, $Argument)
            [pscustomobject]@{{ Execute = $Execute; Arguments = $Argument }}
        }}
        $script:invalid = $false
        function Get-ScheduledTask {{
            param($TaskName, $ErrorAction)
            $kind = if ($TaskName -eq 'LLMWiki-Nightly') {{ 'nightly' }} else {{ 'weekly' }}
            $action = New-LLMWikiScheduledAction `
                -Kind $kind `
                -VaultRoot {ps_literal(root)} `
                -StateRoot {ps_literal(state)} `
                -UvPath {ps_literal(uv_path)} `
                -RunnerPath {ps_literal(runner)} `
                -PowerShellPath 'pwsh.exe'
            [pscustomobject]@{{
                State = 'Ready'
                Actions = @($action)
                Triggers = @([pscustomobject]@{{
                    StartBoundary = '2026-08-15T03:00:00'
                    Enabled = $true
                }})
                Principal = [pscustomobject]@{{
                    LogonType = $(if ($script:invalid) {{ 'ServiceAccount' }} else {{ 'Interactive' }})
                    UserId = 'operator'
                }}
            }}
        }}
        function Get-ScheduledTaskInfo {{
            param($InputObject, $ErrorAction)
            process {{ [pscustomobject]@{{
                LastRunTime = [datetime]::MinValue
                LastTaskResult = 0
                NextRunTime = [datetime]::MaxValue
            }} }}
        }}
        $valid = Test-LLMWikiScheduledTasks `
            -VaultRoot {ps_literal(root)} `
            -StateRoot {ps_literal(state)} `
            -UvPath {ps_literal(uv_path)}
        $script:invalid = $true
        $invalid = Test-LLMWikiScheduledTasks `
            -VaultRoot {ps_literal(root)} `
            -StateRoot {ps_literal(state)} `
            -UvPath {ps_literal(uv_path)}
        @([bool]$valid, [bool]$invalid) | ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        # pwsh startup alone can take ten seconds on a loaded hosted runner.
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [True, False], result.stdout
