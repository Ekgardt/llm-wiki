"""The Claude provider frames the task, so a machine's session banner is not answered instead.

A `SessionStart` hook from managed settings runs in every `claude -p` call and its output
reaches the model before the prompt. Research:
`docs/research/2026-09-17-the-task-is-named-to-the-model.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm_client  # noqa: E402

MODERN = frozenset({"--system-prompt", "--append-system-prompt"})


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


@pytest.mark.parametrize("flags", [MODERN, frozenset()])
@pytest.mark.parametrize("system_prompt", ["BE A JUDGE", ""])
def test_the_prompt_always_travels_inside_the_task_frame(monkeypatch, flags, system_prompt) -> None:
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: flags)

    assert llm_client._claude_stdin(system_prompt, "is it so?").endswith("<task>\nis it so?\n</task>")


def test_the_system_prompt_names_the_frame(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: MODERN)

    system_text = _flag_value(llm_client._claude_command("/bin/claude", None, "BE A JUDGE"), "--system-prompt")

    assert system_text == f"BE A JUDGE\n\n{llm_client.TASK_FRAME}"


def test_a_call_without_a_system_prompt_appends_the_frame_and_keeps_the_persona(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: MODERN)

    command = llm_client._claude_command("/bin/claude", None, "")

    assert ("--system-prompt" in command, _flag_value(command, "--append-system-prompt")) == (
        False,
        llm_client.TASK_FRAME,
    )


def test_an_older_cli_reads_the_frame_from_the_prompt(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset())

    sent = llm_client._claude_stdin("", "is it so?")

    assert sent.startswith(f"<system>{llm_client.TASK_FRAME}</system>")


@pytest.mark.parametrize("system_prompt", ["BE A JUDGE", ""])
def test_codex_carries_the_same_frame(system_prompt) -> None:
    """Codex reads instruction files of its own before the prompt."""
    sent = llm_client._codex_prompt(system_prompt, "is it so?")

    assert sent.startswith(f"SYSTEM: {llm_client._framed_system_text(system_prompt)}")
    assert sent.endswith("USER: <task>\nis it so?\n</task>")


def test_opencode_carries_the_same_frame(monkeypatch) -> None:
    """An OpenCode session prepends whatever its configuration and plugins hold."""
    sent: dict = {}
    monkeypatch.setattr(
        llm_client, "_opencode_post", lambda url, body: sent.update(body) or {}
    )
    monkeypatch.setattr(llm_client, "_opencode_text", lambda data: "ok")
    monkeypatch.setattr(llm_client, "_parse_opencode_usage", lambda data: None)

    llm_client._opencode_answer("http://127.0.0.1:1", "ses", "is it so?", "BE A JUDGE")

    assert sent["parts"] == [{"type": "text", "text": "<task>\nis it so?\n</task>"}]
    assert sent["system"] == f"BE A JUDGE\n\n{llm_client.TASK_FRAME}"
