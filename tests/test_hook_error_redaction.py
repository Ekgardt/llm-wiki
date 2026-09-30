"""Every hook error sink protects credentials and its one-record boundary."""
from __future__ import annotations

import importlib
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("module_name", [
    "session_start_project_state", "session_end_project_tag", "integration_adapter",
])
def test_hook_error_sink_redacts_and_preserves_one_record(tmp_path, monkeypatch, module_name):
    module = importlib.import_module(module_name)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(module, "STATE_ROOT", tmp_path, raising=False)
    secret = "ghp_" + "a" * 36
    message = f"PermissionError: token={secret}\r\n[forged] successful delivery"
    _write_hook_error(module, message)
    text = (tmp_path / "logs" / "hook-errors.log").read_text(encoding="utf-8")
    assert secret not in text
    assert len(text.splitlines()) == 1
    assert "PermissionError" in text
    assert "successful delivery" in text


def _write_hook_error(module, message):
    if module.__name__ == "integration_adapter":
        module._log_hook_error("audit diagnostic", message)
        return
    module._safe_write_error(message)


def test_bootstrap_child_failure_keeps_redacted_reason(tmp_path, monkeypatch):
    module = importlib.import_module("session_start_project_state")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    secret = "ghp_" + "b" * 36
    (scripts / "bootstrap_project.py").write_text(
        f"import sys\nprint('writer unavailable token={secret}', file=sys.stderr)\n"
        "raise SystemExit(7)\n", encoding="utf-8",
    )
    module._bootstrap_new_project(tmp_path, tmp_path, tmp_path / "project" / "state.md")
    text = (tmp_path / "logs" / "hook-errors.log").read_text(encoding="utf-8")
    assert "CalledProcessError" in text
    assert "7" in text
    assert "writer unavailable" in text
    assert secret not in text
    assert len(text.splitlines()) == 1


def test_bootstrap_timeout_keeps_its_cause(tmp_path, monkeypatch):
    module = importlib.import_module("session_start_project_state")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    def expire(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"writer busy")

    monkeypatch.setattr(subprocess, "run", expire)
    module._bootstrap_new_project(tmp_path, tmp_path, tmp_path / "project" / "state.md")
    text = (tmp_path / "logs" / "hook-errors.log").read_text(encoding="utf-8")
    assert "TimeoutExpired" in text
    assert "writer busy" in text


def test_successful_bootstrap_has_no_failure_log(tmp_path, monkeypatch):
    module = importlib.import_module("session_start_project_state")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bootstrap_project.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    module._bootstrap_new_project(tmp_path, tmp_path, tmp_path / "project" / "state.md")
    assert not (tmp_path / "logs" / "hook-errors.log").exists()


@pytest.mark.parametrize("journal_present", [False, True])
def test_later_session_completes_bootstrap_missing_after_failure(tmp_path, monkeypatch, journal_present):
    module = importlib.import_module("session_start_project_state")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    project = tmp_path / "checkout"
    project.mkdir()
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    projects = tmp_path / "knowledge" / "projects"
    state = projects / "demo" / "state.md"
    state.parent.mkdir(parents=True)
    state.write_text("retained project state", encoding="utf-8")
    if journal_present:
        (state.parent / "journal.md").write_text("retained journal", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    child = scripts / "bootstrap_project.py"
    child.write_text("raise SystemExit(7)\n", encoding="utf-8")
    module._bootstrap_new_project(tmp_path, project, state)
    target = state.parent / "bootstrap.md"
    child.write_text(
        f"from pathlib import Path\nPath({str(target)!r}).write_text('recovered context')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ProjectStore", lambda *_args: None)
    handoff = SimpleNamespace(context="journal context", degraded=False, legacy=False)
    monkeypatch.setattr(module, "recover_project_handoff", lambda *_args, **_kwargs: handoff)
    monkeypatch.setattr(module, "_emit", lambda _context: 0)
    assert module._emit_project_context(tmp_path, projects, project, "demo") == 0
    assert target.read_text(encoding="utf-8") == "recovered context"
    assert state.read_text(encoding="utf-8") == "retained project state"


def test_completed_bootstrap_is_not_relaunched(tmp_path, monkeypatch):
    module = importlib.import_module("session_start_project_state")
    (tmp_path / "bootstrap.md").write_text("existing context", encoding="utf-8")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("completed bootstrap must not launch a child")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(module, "_safe_write_error", unexpected)
    module._bootstrap_new_project(tmp_path, tmp_path, tmp_path / "state.md")


def test_shared_hook_error_writer_reports_unwritable_destination(tmp_path):
    from capture_diagnostics import record_hook_error

    (tmp_path / "logs").write_bytes(b"not a directory")
    assert record_hook_error(tmp_path, "capture", "failed") is False


def test_shared_hook_error_writer_protects_the_kind_as_well_as_the_message(tmp_path):
    from capture_diagnostics import record_hook_error

    secret = "ghp_" + "d" * 36
    assert record_hook_error(tmp_path, f"event\ntoken={secret}", f"failure token={secret}")
    text = (tmp_path / "logs/hook-errors.log").read_text()
    assert len(text.splitlines()) == 1
    assert secret not in text
    assert "failure" in text


def test_shared_hook_error_writer_does_not_guess_a_missing_root():
    from capture_diagnostics import record_hook_error

    assert record_hook_error(None, "capture", "failed") is False
