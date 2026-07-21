"""Shared code-kernel fixture and helper contract tests."""

from __future__ import annotations

from pathlib import Path

from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_python_repository, fixture_digest


def test_fixture_copy_is_git_scoped_and_deterministic(tmp_path: Path) -> None:
    first = create_python_repository(tmp_path / "first")
    second = create_python_repository(tmp_path / "second")

    assert fixture_digest(first) == fixture_digest(second)
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
