"""Focused contract tests for Cursor and Antigravity user hooks."""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import capture_operation  # noqa: E402
import integration_adapter  # noqa: E402


def _cursor_commands(template: dict[str, object]) -> list[str]:
    hooks = template["hooks"]
    assert isinstance(hooks, dict)
    return [handlers[0]["command"] for handlers in hooks.values()]


def _antigravity_commands(template: dict[str, object]) -> list[str]:
    hooks = template["llm-wiki"]
    assert isinstance(hooks, dict)
    return [
        hooks["PreInvocation"][0]["command"],
        hooks["PostToolUse"][0]["hooks"][0]["command"],
        hooks["Stop"][0]["command"],
    ]


def _assert_adapter_commands(commands: list[str], expected_count: int) -> None:
    assert len(commands) == expected_count
    assert all("uv run --locked --no-sync --directory" in command for command in commands)
    assert all("__LLM_WIKI_ROOT__" in command for command in commands)
    assert all("scripts/integration_adapter.py" in command for command in commands)
    assert all("--delegate" not in command for command in commands)


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    event: str,
    raw: str,
) -> dict[str, object]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert integration_adapter.main(["--source", source, "--event", event]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_cursor_template_uses_official_user_hook_shape_and_bounded_adapter_commands():
    template = json.loads(
        (ROOT / "integrations" / "cursor" / "hooks.json").read_text(encoding="utf-8")
    )

    assert template["version"] == 1
    assert set(template["hooks"]) == {
        "sessionStart",
        "beforeSubmitPrompt",
        "postToolUse",
        "preCompact",
        "stop",
        "sessionEnd",
    }
    assert all(isinstance(handlers, list) for handlers in template["hooks"].values())
    _assert_adapter_commands(_cursor_commands(template), 6)


def test_antigravity_template_uses_named_hook_and_significant_tool_group():
    template = json.loads(
        (ROOT / "integrations" / "antigravity" / "hooks.json").read_text(encoding="utf-8")
    )

    assert set(template) == {"llm-wiki"}
    hooks = template["llm-wiki"]
    assert set(hooks) == {"PreInvocation", "PostToolUse", "Stop"}
    assert hooks["PostToolUse"][0]["matcher"] == (
        "write_to_file|replace_file_content|multi_replace_file_content|run_command"
    )
    assert "hooks" in hooks["PostToolUse"][0]
    assert "hooks" not in hooks["PreInvocation"][0]
    assert "hooks" not in hooks["Stop"][0]
    _assert_adapter_commands(_antigravity_commands(template), 3)


def test_ide_guidance_uses_mcp_decisions_instead_of_direct_markdown_append():
    guidance = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "integrations" / "cursor" / "rules" / "llm-wiki.mdc",
            ROOT / "integrations" / "antigravity" / "AGENTS.md",
        )
    )

    assert "log_decision" in guidance
    assert "daily_log_append.py" not in guidance


def test_cursor_projection_accepts_official_shape_and_excludes_untrusted_fields():
    raw = {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "session_id": "session-ignored",
        "workspace_roots": ["C:/workspace"],
        "cwd": "C:/workspace/project",
        "tool_name": "Write",
        "tool_input": {"file_path": "src/app.py", "content": "do not retain"},
        "tool_use_id": "tool-1",
        "tool_output": "do not retain",
        "attachments": [{"file_path": "secret.txt"}],
        "agent": "spoofed-agent",
        "project_delta": {"goal": "do not trust"},
    }

    envelope = integration_adapter.normalize_host_event("cursor", "post_tool_use", raw)

    assert envelope is not None
    assert envelope.agent == "cursor"
    assert envelope.session == "conversation-1"
    assert envelope.worktree == "C:/workspace/project"
    assert envelope.source_event_id == "tool-1"
    assert dict(envelope.payload) == {
        "tool_name": "Write",
        "target": "src/app.py",
        "changed": True,
        "dirty": True,
        "significant": True,
    }
    serialized = envelope.to_json()
    assert "do not retain" not in serialized
    assert "attachments" not in serialized
    assert "project_delta" not in serialized


def test_cursor_multiroot_without_event_cwd_omits_project_binding():
    envelope = integration_adapter.normalize_host_event(
        "cursor",
        "session_start",
        {
            "conversation_id": "conversation-1",
            "generation_id": "generation-1",
            "workspace_roots": ["C:/one", "C:/two"],
        },
    )

    assert envelope is not None
    assert envelope.worktree is None
    assert envelope.project is None


def test_antigravity_projection_maps_tools_without_outputs_artifacts_or_full_errors():
    envelope = integration_adapter.normalize_host_event(
        "antigravity",
        "post_tool_use",
        {
            "conversationId": "conversation-1",
            "workspacePaths": ["C:/workspace"],
            "artifactDirectoryPath": "C:/private/artifacts",
            "transcriptPath": "C:/private/transcript.jsonl",
            "stepIdx": 7,
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "uv run pytest", "Cwd": "C:/workspace/project"},
            },
            "toolOutput": "do not retain",
            "error": "full private error details",
        },
    )

    assert envelope is not None
    assert envelope.agent == "antigravity"
    assert envelope.session == "conversation-1"
    assert envelope.worktree == "C:/workspace/project"
    assert envelope.payload["tool_name"] == "Bash"
    assert envelope.payload["target"] == "uv run pytest"
    assert envelope.payload["significant_failure"] is True
    assert "full private error details" not in envelope.to_json()
    assert "artifact" not in envelope.to_json().lower()
    assert "toolOutput" not in envelope.to_json()


def test_host_occurrence_ids_are_replay_stable_and_distinguish_occurrences():
    cursor_raw = {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "workspace_roots": ["C:/workspace"],
        "prompt": "remember this",
    }
    cursor_first = integration_adapter.normalize_host_event("cursor", "user_prompt", cursor_raw)
    cursor_replay = integration_adapter.normalize_host_event("cursor", "user_prompt", cursor_raw)
    cursor_next = integration_adapter.normalize_host_event(
        "cursor", "user_prompt", {**cursor_raw, "generation_id": "generation-2"}
    )
    anti_raw = {
        "conversationId": "conversation-1",
        "workspacePaths": ["C:/workspace"],
        "toolCall": {"name": "write_to_file", "args": {"TargetFile": "a.py"}},
        "stepIdx": 3,
    }
    anti_first = integration_adapter.normalize_host_event("antigravity", "post_tool_use", anti_raw)
    anti_next = integration_adapter.normalize_host_event(
        "antigravity", "post_tool_use", {**anti_raw, "stepIdx": 4}
    )

    assert cursor_first is not None
    assert cursor_replay is not None
    assert cursor_next is not None
    assert anti_first is not None
    assert anti_next is not None
    assert cursor_first.source_event_id == "generation-1"
    assert cursor_first.event_id == cursor_replay.event_id
    assert cursor_first.event_id != cursor_next.event_id
    assert re.fullmatch(r"[0-9a-f]{64}", anti_first.source_event_id or "")
    assert anti_first.event_id != anti_next.event_id


def test_raw_source_event_id_is_supported_and_redacted_before_identity():
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    envelope = integration_adapter.normalize_host_event(
        "cursor",
        "user_prompt",
        {
            "conversation_id": "conversation-1",
            "source_event_id": f"event-{secret}",
            "workspace_roots": ["C:/workspace"],
            "prompt": f"token {secret}",
        },
    )

    assert envelope is not None
    assert secret not in envelope.to_json()
    assert secret not in (envelope.source_event_id or "")


def test_antigravity_protocol_maps_initial_invocation_and_idle_stop():
    common = {"conversationId": "conversation-1", "workspacePaths": ["C:/workspace"]}

    initial = integration_adapter.normalize_host_event(
        "antigravity", "session_start", {**common, "invocationNum": 0}
    )
    later = integration_adapter.normalize_host_event(
        "antigravity", "session_start", {**common, "invocationNum": 1}
    )
    idle = integration_adapter.normalize_host_event(
        "antigravity",
        "stop",
        {**common, "executionNum": 2, "fullyIdle": True, "terminationReason": "done"},
    )
    active = integration_adapter.normalize_host_event(
        "antigravity",
        "stop",
        {**common, "executionNum": 2, "fullyIdle": False, "terminationReason": "tasks"},
    )

    assert initial is not None and initial.event_type == "session_start"
    assert later is None
    assert idle is not None and idle.event_type == "session_end"
    assert active is not None and active.event_type == "stop"


@pytest.mark.parametrize(
    ("source", "event", "raw", "expected"),
    [
        ("cursor", "session_start", "{", {}),
        ("cursor", "user_prompt", "{", {"continue": True}),
        ("cursor", "post_tool_use", "{", {}),
        ("antigravity", "session_start", "{", {}),
        ("antigravity", "post_tool_use", "{", {}),
        ("antigravity", "stop", "{", {"decision": "stop"}),
    ],
)
def test_malformed_host_input_emits_exactly_one_neutral_json_object(
    monkeypatch, capsys, source, event, raw, expected
):
    assert _run_main(monkeypatch, capsys, source, event, raw) == expected


def test_oversize_host_input_emits_neutral_json_without_echo(monkeypatch, capsys):
    oversized = "x" * (integration_adapter.MAX_STDIN_BYTES + 1)

    output = _run_main(monkeypatch, capsys, "cursor", "user_prompt", oversized)

    assert output == {"continue": True}


@pytest.mark.parametrize(
    ("source", "event", "raw", "capture_result", "expected"),
    [
        (
            "cursor",
            "session_start",
            {
                "conversation_id": "conversation-1",
                "generation_id": "generation-1",
                "workspace_roots": ["C:/workspace"],
            },
            {"context": "bounded context"},
            {"additional_context": "bounded context"},
        ),
        (
            "cursor",
            "user_prompt",
            {
                "conversation_id": "conversation-1",
                "generation_id": "generation-1",
                "workspace_roots": ["C:/workspace"],
                "prompt": "hello",
            },
            {},
            {"continue": True},
        ),
        (
            "antigravity",
            "session_start",
            {
                "conversationId": "conversation-1",
                "workspacePaths": ["C:/workspace"],
                "invocationNum": 0,
            },
            {"context": "bounded context"},
            {"injectSteps": [{"ephemeralMessage": "bounded context"}]},
        ),
        (
            "antigravity",
            "stop",
            {
                "conversationId": "conversation-1",
                "workspacePaths": ["C:/workspace"],
                "executionNum": 1,
                "fullyIdle": False,
                "terminationReason": "background work",
            },
            {},
            {"decision": "stop"},
        ),
    ],
)
def test_successful_host_events_emit_official_json_output(
    monkeypatch, capsys, source, event, raw, capture_result, expected
):
    monkeypatch.setattr(integration_adapter, "ingest_event", lambda envelope: capture_result)

    output = _run_main(monkeypatch, capsys, source, event, json.dumps(raw))

    assert output == expected


def test_canonical_event_id_replays_through_existing_capture_operation():
    envelope = integration_adapter.normalize_host_event(
        "cursor",
        "post_tool_use",
        {
            "conversation_id": "conversation-1",
            "generation_id": "generation-1",
            "workspace_roots": ["C:/workspace"],
            "tool_name": "Write",
            "tool_input": {"file_path": "src/app.py"},
            "tool_use_id": "tool-1",
        },
    )
    assert envelope is not None
    payload = integration_adapter._canonical_capture_payload(envelope)
    state: dict[str, object] = {}

    def update(mutator):
        mutator(state)
        return state

    kwargs = {
        "namespace": "ide-hook-replay",
        "key": "src/app.py",
        "prefix": "tool",
        "source_event_id": payload["event_id"],
        "rate_limit_seconds": 60,
        "max_entries": 10,
        "now": datetime(2026, 8, 16, 12, 0, 0),
    }
    first = capture_operation.claim_operation(update, **kwargs)
    assert first is not None
    capture_operation.complete_operation(
        update,
        namespace="ide-hook-replay",
        key="src/app.py",
        operation_id=first,
        now=kwargs["now"],
    )

    assert payload["event_id"] == envelope.event_id
    assert payload["event_id"] != envelope.source_event_id
    assert capture_operation.claim_operation(update, **kwargs) == first


def test_feedback_trigger_uses_canonical_source(monkeypatch):
    calls = []
    envelope = integration_adapter.normalize_host_event(
        "cursor",
        "user_prompt",
        {
            "conversation_id": "conversation-1",
            "generation_id": "generation-1",
            "workspace_roots": ["C:/workspace"],
            "prompt": "remember this",
        },
    )
    assert envelope is not None
    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda event: None)
    monkeypatch.setattr(
        integration_adapter, "_project_context", lambda event: ("demo", Path("C:/workspace"))
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: calls.append((name, payload, kwargs)),
    )

    integration_adapter.ingest_event(envelope)

    assert calls[1][1]["trigger"] == "cursor-user-message"
    assert calls[0][1]["event_id"] == envelope.event_id
