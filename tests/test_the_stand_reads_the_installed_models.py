"""A benchmark stand opens the checkout's verified models, not an empty state root."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "scripts", ROOT / "benchmark"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import longmemeval_vault  # noqa: E402
import memory_state  # noqa: E402
import model_files  # noqa: E402

REPO_ID = "owner/encoder"
REVISION = "0123456789abcdef"


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts/integration_adapter.py").write_bytes(
        (ROOT / "scripts/integration_adapter.py").read_bytes()
    )
    model = checkout / "cache/models" / REPO_ID.replace("/", "--") / REVISION
    model.mkdir(parents=True)
    (model / "model.onnx").write_bytes(b"weights")
    return checkout


def _prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(longmemeval_vault, "REPO", _checkout(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT", "MEMORY_LLM_PROVIDER"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.chdir(tmp_path)
    _root, state = longmemeval_vault.prepare_environment(tmp_path / "stand")
    monkeypatch.setattr(memory_state, "STATE_ROOT", state)
    return state


def test_the_stand_resolves_an_installed_model_under_its_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _prepared(tmp_path, monkeypatch)

    found = model_files.installed_file(REPO_ID, REVISION, "model.onnx")

    assert model_files.models_root() == state / "cache/models"
    assert found.read_bytes() == b"weights"


def test_a_stand_that_has_its_own_models_keeps_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "stand/state/cache/models"
    own.mkdir(parents=True)

    _prepared(tmp_path, monkeypatch)

    assert not own.is_symlink()
    assert list(own.iterdir()) == []
