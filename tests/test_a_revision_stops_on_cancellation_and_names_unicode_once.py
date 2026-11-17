"""A workspace revision stops when its caller cancels, and names a Unicode path once."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import compute_workspace_revision

from tests.workspace_revision_helpers import isolate_global_git_configuration


@pytest.fixture(autouse=True)
def _isolated_global_git_configuration(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_global_git_configuration(tmp_path_factory, monkeypatch)


def test_revision_checks_cancellation_during_manifest_hashing(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.py").write_text(str(index), encoding="utf-8")
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 4

    with pytest.raises(TimeoutError, match="cancel"):
        compute_workspace_revision(resolve_repository_scope(root), cancelled=cancelled)

    assert calls == 4

def test_revision_checks_cancellation_during_recursive_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    first = root / "first.py"
    second = root / "second.py"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    scope = resolve_repository_scope(root)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return False

    def paths(_path: Path, _pattern: str):
        yield first
        if calls < 2:
            raise AssertionError("recursive discovery did not check cancellation")
        yield second

    monkeypatch.setattr(Path, "rglob", paths)

    compute_workspace_revision(scope, cancelled=cancelled)

def test_revision_checks_cancellation_between_hash_chunks(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "large.py").write_bytes(b"x" * (2 * 1024 * 1024))
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 5

    with pytest.raises(TimeoutError, match="cancel"):
        compute_workspace_revision(resolve_repository_scope(root), cancelled=cancelled)

    assert calls == 5

@pytest.mark.skipif(os.name == "nt", reason="distinct NFC-equivalent names require POSIX")
def test_manifest_rejects_unicode_normalization_collisions(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    composed = "caf\u00e9.py"
    decomposed = unicodedata.normalize("NFD", composed)
    (root / composed).write_text("one", encoding="utf-8")
    (root / decomposed).write_text("two", encoding="utf-8")
    if len(list(root.iterdir())) < 2:
        pytest.skip(
            "this filesystem is Unicode-normalization-insensitive, so the two "
            "names are one file and the manifest has no collision to reject"
        )

    with pytest.raises(ValueError, match="normalization collision"):
        compute_workspace_revision(resolve_repository_scope(root))

@pytest.mark.skipif(os.name == "nt", reason="decomposed names require POSIX")
def test_unchanged_verifier_accepts_decomposed_unicode_path(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    composed = "caf\u00e9.py"
    decomposed = unicodedata.normalize("NFD", composed)
    (root / decomposed).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(root)
    expected = compute_workspace_revision(scope)

    assert expected.entries[0].path == composed
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
