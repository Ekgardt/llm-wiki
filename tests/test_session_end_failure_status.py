"""Failed tagging or acknowledgement remains distinguishable from a no-op."""
from __future__ import annotations

import json
import sys

import pytest
import session_end_project_tag as hook


def test_failed_tagging_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    def fail():
        raise OSError("daily writer unavailable")

    monkeypatch.setattr(hook, "_tag_session", fail)
    assert hook.main() == 1
    assert capsys.readouterr().out == ""
    assert "daily writer unavailable" in (tmp_path / "logs/hook-errors.log").read_text()


class _FailedAcknowledgement:
    def __init__(self, mode):
        self.mode = mode

    def write(self, value):
        if self.mode == "write":
            raise BrokenPipeError("acknowledgement pipe closed")
        return len(value)

    def flush(self):
        raise OSError("acknowledgement flush failed")


@pytest.mark.parametrize("mode", ["write", "flush"])
def test_failed_acknowledgement_reports_failure(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(hook, "_tag_session", lambda: True)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", _FailedAcknowledgement(mode))
        result = hook.main()
    assert result == 1
    assert "acknowledgement" in (tmp_path / "logs/hook-errors.log").read_text()


@pytest.mark.parametrize("written", [True, False])
def test_completed_tagging_and_intentional_noop_are_successful(monkeypatch, capsys, written):
    monkeypatch.setattr(hook, "_tag_session", lambda: written)
    assert hook.main() == 0
    assert json.loads(capsys.readouterr().out) == {"daily_log_written": written}
