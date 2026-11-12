"""Helpers shared by test_self_update.py and the test files importing them."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


def _commit(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "--quiet", "-m", f"add {name}")


@pytest.fixture()
def linked_clone(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repository and a clone that tracks its main branch."""
    upstream = _repository(tmp_path / "upstream")
    _commit(upstream, "product.py", "value = 1\n")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(upstream), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return upstream, clone
