"""A context that could not be delivered is not a successful hook result."""
from __future__ import annotations

import json
import sys

import pytest
import session_start_project_state as hook


class _FailingOutput:
    def __init__(self, mode):
        self.mode = mode

    def write(self, value):
        if self.mode == "write":
            raise BrokenPipeError("context pipe closed")
        return len(value)

    def flush(self):
        raise OSError("context flush failed")


@pytest.mark.parametrize("mode", ["write", "flush"])
def test_context_output_failure_returns_failure_and_keeps_cause(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", _FailingOutput(mode))
        result = hook._emit("retained project context")
    assert result == 1
    text = (tmp_path / "logs" / "hook-errors.log").read_text(encoding="utf-8")
    assert "context" in text
    assert len(text.splitlines()) == 1


def test_unhandled_context_error_is_not_an_empty_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    secret = "ghp_" + "c" * 36

    def fail():
        raise RuntimeError(f"context unavailable token={secret}")

    monkeypatch.setattr(hook, "_run_session_start", fail)
    assert hook.main() == 1
    assert capsys.readouterr().out == ""
    text = (tmp_path / "logs" / "hook-errors.log").read_text(encoding="utf-8")
    assert "context unavailable" in text
    assert secret not in text


def test_successful_context_output_remains_valid_hook_json(capsys):
    assert hook._emit("project context") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"] == "project context"
