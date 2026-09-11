"""Codex prompts and edits reach the same capture as Claude's and OpenCode's.

Codex supports `UserPromptSubmit` and `PostToolUse` for `apply_patch` and
`Bash`; until 2026-09-11 the template registered neither for capture
(docs/research/2026-09-11-codex-leaves-breadcrumbs-too.md). Codex adds plain
text printed by a `UserPromptSubmit` hook to the model's context, so the
capture path must print nothing.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch\n"


@pytest.fixture
def adapter(monkeypatch):
    import integration_adapter

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(integration_adapter, "_observe_checkpoint_fail_open", lambda envelope: None)
    monkeypatch.setattr(
        integration_adapter, "_project_context", lambda envelope: ("demo", Path("/work/demo"))
    )
    monkeypatch.setattr(
        integration_adapter,
        "_run_delegate",
        lambda name, payload, **kwargs: calls.append((name, payload)),
    )
    return integration_adapter, calls


def _run(adapter_module, monkeypatch, event: str, payload: dict) -> None:
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    assert adapter_module.main(["--source", "codex", "--event", event]) == 0


def test_a_codex_prompt_runs_prompt_and_feedback_capture_and_prints_nothing(
    adapter, monkeypatch, capsys
):
    adapter_module, calls = adapter
    _run(adapter_module, monkeypatch, "user_prompt", {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "019a-codex-session",
        "turn_id": "turn-1",
        "cwd": "/work/demo",
        "prompt": "Keep this request",
    })

    assert ([name for name, _ in calls], calls[0][1]["prompt"], capsys.readouterr().out) == (
        ["user_prompt_capture.py", "feedback_capture.py"],
        "Keep this request",
        "",
    )


def test_a_codex_patch_is_an_edit_of_the_file_it_touches(adapter, monkeypatch, capsys):
    adapter_module, calls = adapter
    _run(adapter_module, monkeypatch, "post_tool_use", {
        "hook_event_name": "PostToolUse",
        "session_id": "019a-codex-session",
        "cwd": "/work/demo",
        "tool_name": "apply_patch",
        "tool_use_id": "call-1",
        "tool_input": {"command": PATCH},
        "tool_response": {"success": True},
    })

    name, payload = calls[0]
    assert (name, payload["tool_name"], payload["tool_input"]["filePath"]) == (
        "post_tool_capture.py",
        "Edit",
        "src/app.py",
    )
    assert capsys.readouterr().out == ""


def test_the_codex_template_registers_capture_for_prompts_and_edits():
    from codex_hook_identity import is_our_codex_command

    hooks = json.loads((ROOT / "integrations/codex/hooks.json").read_text(encoding="utf-8"))["hooks"]
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    capture = next(group for group in hooks["PostToolUse"] if "apply_patch" in group["matcher"])

    assert (
        prompt["command"].endswith("--source codex --event user_prompt"),
        capture["hooks"][0]["command"].endswith("--source codex --event post_tool_use"),
        is_our_codex_command(prompt["command"]),
        is_our_codex_command(capture["hooks"][0]["commandWindows"]),
    ) == (True, True, True, True)
