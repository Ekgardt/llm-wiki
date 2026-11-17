"""Fixtures the LSP source-containment and log-redaction tests share."""
from __future__ import annotations

from pathlib import Path

import pytest
from repository_scope import RepositoryScope, resolve_repository_scope


@pytest.fixture(name="repository")
def repository_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "api.py").write_text("def api():\n    return 1\n", encoding="utf-8")
    (root / "plain.py").write_text("value = 1\n", encoding="utf-8")
    return root


@pytest.fixture(name="scope")
def scope_fixture(repository: Path) -> RepositoryScope:
    return resolve_repository_scope(repository)


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
