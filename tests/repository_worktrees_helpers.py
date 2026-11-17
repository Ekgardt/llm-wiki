"""Helpers shared by test_repository_worktrees.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

from tests.repository_index_helpers import _git


def _worktree(repository: Path, name: str, branch: str) -> Path:
    path = repository.parent / name
    _git(repository, "worktree", "add", "-q", "-b", branch, str(path))
    return path
