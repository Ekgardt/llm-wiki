"""The OpenCode plugin forwards lifecycle events once, normalization keeps only available signals, and checkpoint observation never blocks durable capture."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime
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

from tests.breadcrumb_storage_helpers import _bundle
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


def test_opencode_plugin_is_lifecycle_only():
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert (
        _unmet_substrings(
            (
                ("integration_adapter.py", plugin, True),
                ("event: async", plugin, True),
                ('"session.created"', plugin, True),
                ('"tool.execute.after"', plugin, True),
                ('"session.idle"', plugin, True),
                ('"experimental.session.compacting"', plugin, True),
                ('"session.created": async', plugin, False),
                ('"session.idle": async', plugin, False),
                ('"memory.context"', plugin, False),
                ('"memory.recall"', plugin, False),
                ("Classify this transcript", plugin, False),
                ("FLUSH_MAJOR", plugin, False),
                ("memory-ephemeral", plugin, False),
                ("client.session.create", plugin, False),
                ("client.session.prompt", plugin, False),
                ("memory_queue.py", plugin, False),
                ("maybe_compile.py", plugin, False),
                ("computeSlug", plugin, False),
                ("state-path", plugin, False),
                ('directory: typeof directory === "string" ? directory : null', plugin, True),
                ("project", plugin, False),
            )
        )
        == []
    )


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
    from integration_hook_config import (
        plugin_with_embedded_root,
    )

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


def test_user_prompt_ingestion_runs_prompt_capture_once(ingress):
    queue, _coordinator = ingress
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

    result = integration_adapter.ingest_event(envelope)
    content = json.loads(_bundle(queue.state_root, result["capture_intent_ids"][0]).content)
    assert content["payload"]["prompt"] == "Preserve this request"
    assert queue.claim_capture("once", handler_versions=(2,)) is not None
    assert queue.claim_capture("twice", handler_versions=(2,)) is None


def test_normalization_preserves_only_available_checkpoint_signals():

    _ensure_scripts_on_path()
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

    assert (
        supplied.payload["dirty"] is True,
        supplied.payload["changed"] is True,
        supplied.payload["public_contract_changed"] is True,
        supplied.payload["token_percent"],
        supplied.payload["compaction_confirmed"] is True,
    ) == (
        True,
        True,
        True,
        70,
        True,
    )
    assert (
        _unmet_substrings(
            (
                ("token_percent", unavailable.payload, False),
                ("compaction_confirmed", unavailable.payload, False),
                ("host_progress_signals", unavailable.payload, False),
            )
        )
        == []
    )


def test_malformed_project_delta_is_rejected_before_durable_enqueue():

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(ValueError, match="invalid project delta") as rejected:
        integration_adapter.normalize_event(
            "claude",
            "post_tool_use",
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "src/app.py"},
                "project_delta": {
                    **project_checkpoint.empty_delta(),
                    "blockers": [{"id": secret, "action": "append", "value": "bad"}],
                },
            },
        )
    assert secret not in str(rejected.value)


def test_significant_file_tool_normalizes_to_file_change_observation():

    _ensure_scripts_on_path()
    from integration_adapter import (
        normalize_event,
    )
    from project_checkpoint import (
        checkpoint_observation,
    )

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

    assert checkpoint_observation(write)["type"] == "file_changed"
    assert checkpoint_observation(write)["significant"] is True
    assert checkpoint_observation(read)["type"] == "post_tool_use"
    assert "significant" not in checkpoint_observation(read)


def test_host_tool_call_id_separates_repeated_mutations():

    _ensure_scripts_on_path()
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


def test_adapter_observes_same_envelope_once_before_durable_capture(monkeypatch, tmp_path):
    import io

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    calls = []
    monkeypatch.setattr(
        project_checkpoint,
        "observe_project_checkpoint",
        _without_writer_wait(lambda envelope: calls.append(("observe", envelope))),
    )
    monkeypatch.setattr(
        integration_capture_records,
        "publish_durable_capture_intent",
        lambda envelope, *_args: calls.append(("capture", envelope)) or "1" * 64,
    )
    monkeypatch.setattr(integration_delegates, "run_delegate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda _args: 123)
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

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

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

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))
    event_raw = {"session_id": "s1", "cwd": "C:/project", **raw}

    first = integration_adapter.normalize_occurrence_event("claude", event_type, event_raw)
    second = integration_adapter.normalize_occurrence_event("claude", event_type, event_raw)
    project_checkpoint.observe_project_checkpoint(first)
    project_checkpoint.observe_project_checkpoint(second)

    assert (
        first.event_id != second.event_id,
        first.source_event_id,
        second.source_event_id,
    ) == (
        True,
        first.payload["occurrence_id"],
        second.payload["occurrence_id"],
    )
    uuid.UUID(first.payload["occurrence_id"])
    uuid.UUID(second.payload["occurrence_id"])
    assert [checkpoint["reason"] for checkpoint in checkpoints] == [reason, reason]
    # A checkpoint is named after the batch it commits, not after one of its
    # members — see `batch_occurrence_id`. Each of these batches holds one
    # event, and the two names must still differ.
    assert [checkpoint["occurrence_id"] for checkpoint in checkpoints] == [
        project_checkpoint.batch_occurrence_id([first.event_id]),
        project_checkpoint.batch_occurrence_id([second.event_id]),
    ]


def test_same_normalized_occurrence_is_checkpointed_once(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

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

    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))
    envelope = integration_adapter.normalize_occurrence_event(
        "claude",
        "pre_compact",
        {"session_id": "s1", "cwd": "C:/project", "reason": "auto"},
    )

    project_checkpoint.observe_project_checkpoint(envelope)
    project_checkpoint.observe_project_checkpoint(envelope)

    assert len(checkpoints) == 1
    assert checkpoints[0]["occurrence_id"] == project_checkpoint.batch_occurrence_id(
        [envelope.event_id]
    )


def test_durable_capture_runs_when_checkpoint_observation_fails(monkeypatch, capsys, tmp_path):
    import io

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    calls = []
    monkeypatch.setattr(
        project_checkpoint,
        "observe_project_checkpoint",
        _without_writer_wait(lambda envelope: (_ for _ in ()).throw(RuntimeError("x" * 2000))),
    )

    def publish(*_args):
        calls.append("capture")
        return "1" * 64

    monkeypatch.setattr(
        integration_delegates,
        "run_delegate",
        lambda *args, **kwargs: calls.append(args[0]),
    )
    monkeypatch.setattr(integration_capture_records, "publish_durable_capture_intent", publish)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda _args: 123)
    monkeypatch.setattr(
        project_checkpoint,
        "log_checkpoint_error",
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
        ),
        calls,
    ) == (
        0,
        [("logged", 2000), "capture", "session_end_project_tag.py"],
    )
    assert "capture skipped" not in capsys.readouterr().err


def test_checkpoint_error_text_is_single_line_redacted_and_bounded():

    _ensure_scripts_on_path()
    import project_checkpoint

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    text = project_checkpoint.bounded_checkpoint_error(
        RuntimeError(f"first\nAuthorization: Bearer {secret}" + "x" * 2000)
    )

    assert "\n" not in text
    assert secret not in text
    assert len(text) <= project_checkpoint.MAX_CHECKPOINT_ERROR_CHARS


def test_adapter_observes_before_direct_ingestion(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    envelope = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/project"}
    )
    calls = []
    monkeypatch.setattr(
        project_checkpoint,
        "observe_project_checkpoint",
        _without_writer_wait(lambda observed: calls.append(("observe", observed.event_id))),
    )
    monkeypatch.setattr(
        integration_session_start,
        "record_activity",
        lambda *args: calls.append(("ingest", args[0].event_id)) or True,
    )
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))

    integration_adapter.ingest_event(envelope)
    assert calls == [("observe", envelope.event_id), ("ingest", envelope.event_id)]


def test_direct_ingestion_continues_when_checkpoint_observation_fails(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    envelope = integration_adapter.normalize_event(
        "codex", "session_end", {"session_id": "s1", "cwd": "C:/project"}
    )
    calls = []
    monkeypatch.setattr(
        project_checkpoint,
        "observe_project_checkpoint",
        _without_writer_wait(lambda observed: (_ for _ in ()).throw(RuntimeError("checkpoint failed"))),
    )
    monkeypatch.setattr(
        project_checkpoint,
        "log_checkpoint_error",
        lambda error: calls.append("logged"),
        raising=False,
    )
    monkeypatch.setattr(
        integration_session_start,
        "record_activity",
        lambda *args: calls.append("ingested") or True,
    )
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))

    result = integration_adapter.ingest_event(envelope)
    assert calls == ["logged", "ingested"]
    assert result["heartbeat_recorded"] is True


def test_claude_stop_is_dirty_checkpoint_only_and_never_dispatches_session_end(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

    calls = []
    monkeypatch.setattr(
        project_checkpoint,
        "observe_project_checkpoint",
        _without_writer_wait(lambda envelope: calls.append(("observe", envelope.event_type))),
    )
    monkeypatch.setattr(
        integration_delegates,
        "run_delegate",
        lambda name, *args, **kwargs: calls.append(("delegate", name)),
    )
    envelope = integration_adapter.normalize_event(
        "claude",
        "stop",
        {"session_id": "s1", "cwd": "C:/project", "dirty": True},
    )

    result = integration_adapter.ingest_event(envelope)

    assert (
        envelope.event_type,
        project_checkpoint.checkpoint_observation(envelope),
        calls,
        result["daily_log_written"] is False,
        result["flush_spawned"] is False,
    ) == (
        "stop",
        {"type": "stop", "event_id": envelope.event_id, "dirty": True},
        [("observe", "stop")],
        True,
        True,
    )


def test_failed_checkpoint_does_not_persist_event_dedupe_and_retry_succeeds(monkeypatch):

    _ensure_scripts_on_path()
    import integration_adapter
    import project_checkpoint

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
        occurred_at=datetime.fromisoformat("2026-07-13T12:00:00+00:00"),
    )
    monkeypatch.setattr(project_checkpoint, "update_state", update)
    monkeypatch.setattr(integration_session_start, "ProjectStore", Store)
    monkeypatch.setattr(project_checkpoint, "ProjectStore", Store)
    monkeypatch.setattr(event_project, "project_context", lambda event: ("demo", ROOT))

    with pytest.raises(RuntimeError, match="temporary checkpoint failure"):
        project_checkpoint.observe_project_checkpoint(envelope)
    assert state.get("project_checkpoint_reducers", {}) == {}

    project_checkpoint.observe_project_checkpoint(envelope)
    reducer_state = state["project_checkpoint_reducers"]["demo:s1"]
    assert envelope.event_id in reducer_state["observed_event_ids"]
    assert len(attempts) == 2
