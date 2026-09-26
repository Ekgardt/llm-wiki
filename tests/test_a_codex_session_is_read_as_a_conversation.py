"""A Codex rollout renders as the conversation it records.

Every Codex line is `{"type": "response_item", "payload": {...}}`; the renderer
read only Claude's `user`/`assistant` lines, so a Codex session left no record
and gave the classifier no text. The shapes below follow the Codex source. See
docs/research/2026-09-25-a-codex-session-is-read-as-a-conversation.md.
"""

from __future__ import annotations

import json

from session_evidence import render_transcript


def _line(item_type: str, payload: dict) -> str:
    return json.dumps({"timestamp": "2026-09-25T10:00:00.000Z", "type": item_type, "payload": payload})


def _message(role: str, part: str, text: str) -> str:
    return _line("response_item", {"type": "message", "role": role, "content": [{"type": part, "text": text}]})


ROLLOUT = "\n".join(
    [
        _line("session_meta", {"id": "s1", "cwd": "/work"}),
        _message("developer", "input_text", "system instructions"),
        _message("user", "input_text", "Fix the failing test"),
        _line("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "x"}),
        _line(
            "response_item",
            {"type": "function_call", "name": "shell", "call_id": "c1",
             "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]})},
        ),
        _line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "1 failed"}),
        _line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "input": "*** Begin Patch"}),
        _message("assistant", "output_text", "The test passes now."),
        _line("event_msg", {"type": "user_message", "message": "Fix the failing test"}),
    ]
)


def test_a_codex_rollout_renders_its_turns_and_tool_calls() -> None:
    assert render_transcript(ROLLOUT).split("\n\n") == [
        "**user:** Fix the failing test",
        "- tool `shell`: bash -lc pytest -q",
        "- tool `apply_patch`",
        "**assistant:** The test passes now.",
    ]


def test_a_claude_transcript_renders_as_before() -> None:
    line = json.dumps({"type": "user", "message": {"content": "hello"}})

    assert render_transcript(line) == "**user:** hello"
