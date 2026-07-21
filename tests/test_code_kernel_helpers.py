"""Shared code-kernel fixture and helper contract tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_python_repository, fixture_digest


def test_fixture_copy_is_git_scoped_and_deterministic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+0000")
    first = create_python_repository(tmp_path / "first")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2002-02-02T00:00:00+0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2002-02-02T00:00:00+0000")
    second = create_python_repository(tmp_path / "second")

    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=first,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    second_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=second,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()

    assert fixture_digest(first) == fixture_digest(second)
    assert first_head == second_head
    assert (
        resolve_repository_scope(first).repository_id
        != resolve_repository_scope(second).repository_id
    )
    assert (first / "pkg/api.py").read_text(encoding="utf-8").startswith(
        "class PublicApi"
    )


def test_shared_plugin_provides_independent_state_and_repository_fixtures(
    state_root: Path,
    repository: Path,
    pytestconfig,
) -> None:
    assert state_root.is_dir()
    assert (repository / ".git").exists()
    assert state_root not in repository.parents
    assert pytestconfig.pluginmanager.hasplugin("tests.code_kernel_helpers")


def test_fixture_repository_ignores_ambient_git_config_and_templates(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    global_config = home / ".gitconfig"
    global_config.write_text(
        "[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = definitely-missing-gpg\n",
        encoding="utf-8",
    )
    hostile_template = tmp_path / "hostile-template"
    hostile_hooks = hostile_template / "hooks"
    hostile_hooks.mkdir(parents=True)
    hook = hostile_hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(hostile_template))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgSign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    repository = create_python_repository(tmp_path / "repository")

    assert global_config.read_text(encoding="utf-8").startswith("[commit]")
    assert not (repository / ".git" / "hooks" / "pre-commit").exists()
