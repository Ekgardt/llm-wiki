"""Automatic bootstrap respects another writer and reports actual commit state."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import bootstrap_project as bootstrap
import pytest
import session_start_project_state as hook


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    projects = tmp_path / "knowledge" / "projects"
    page = projects / "demo" / "bootstrap.md"
    page.parent.mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "PROJECTS_DIR", projects)
    monkeypatch.setattr(bootstrap, "_compute_slug", lambda _cwd: "demo")
    monkeypatch.setattr(bootstrap, "_bootstrap_content", lambda *_args: "generated context")
    return page


def test_automatic_bootstrap_preserves_a_page_created_after_parent_check(target, tmp_path, monkeypatch):
    def child(command, **_kwargs):
        target.write_bytes(b"concurrent accepted context")
        options = {}
        if "--if-missing" in command:
            options["if_missing"] = True
        result = bootstrap.bootstrap(str(tmp_path), apply=True, **options)
        return subprocess.CompletedProcess(command, 0, result.encode(), b"")

    monkeypatch.setattr(subprocess, "run", child)
    hook._bootstrap_new_project(tmp_path, tmp_path, target.with_name("state.md"))
    assert target.read_bytes() == b"concurrent accepted context"


@pytest.mark.parametrize("state", ["conflicted", "quarantined", "prepared", "discarded"])
def test_bootstrap_never_reports_written_for_noncommitted_transaction(target, tmp_path, monkeypatch, state):
    monkeypatch.setattr(bootstrap, "mutate_knowledge", lambda *_args, **_kwargs: SimpleNamespace(state=state))
    with pytest.raises(RuntimeError, match=state):
        bootstrap.bootstrap(str(tmp_path), apply=True)


def test_create_only_publication_checks_absence_at_transaction_boundary(target, tmp_path, monkeypatch):
    def concurrent_page(_slug, _content):
        target.write_bytes(b"concurrent accepted context")
        return "new generated context"

    monkeypatch.setattr(bootstrap, "_bootstrap_page", concurrent_page)
    with pytest.raises(ValueError, match="precondition"):
        bootstrap.bootstrap(str(tmp_path), apply=True, if_missing=True)
    assert target.read_bytes() == b"concurrent accepted context"


def test_create_only_bootstrap_commits_once_and_manual_refresh_remains_available(target, tmp_path, monkeypatch):
    assert bootstrap.bootstrap(str(tmp_path), apply=True, if_missing=True).startswith("Written:")
    first = target.read_bytes()
    monkeypatch.setattr(bootstrap, "_bootstrap_content", lambda *_args: "refreshed context")
    assert bootstrap.bootstrap(str(tmp_path), apply=True, if_missing=True).startswith("Exists:")
    assert target.read_bytes() == first
    assert bootstrap.bootstrap(str(tmp_path), apply=True).startswith("Written:")
    assert "refreshed context" in target.read_text(encoding="utf-8")


def test_real_cli_preserves_existing_bootstrap_in_automatic_mode(target, tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    target.write_bytes(b"accepted existing context")
    result = subprocess.run(
        [sys.executable, bootstrap.__file__, "--cwd", str(project), "--apply", "--if-missing"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.startswith("Exists:")
    assert target.read_bytes() == b"accepted existing context"
