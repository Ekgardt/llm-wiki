"""Helpers shared by test_repository_index.py and the test files importing them."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


def _repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


ALPHA = "def helper(value):\n    return value + 1\n\n\ndef caller(value):\n    return helper(value) * 2\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """An isolated vault and state root; nothing here touches the live one."""
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))

    return root, state
