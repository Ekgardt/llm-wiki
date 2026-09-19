"""The freshness proof covers what the corpus covers, and reads git against the disk.

Third audit: the second half of G-H3, and G-M8 points 1 and 2. Research:
`docs/research/2026-09-17-a-revision-walks-what-the-corpus-walks.md`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


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


def _revision(root: Path):
    from repository_scope import resolve_repository_scope
    from workspace_revision import compute_workspace_revision

    return compute_workspace_revision(resolve_repository_scope(root))


def _verified(root: Path, revision) -> bool:
    from repository_scope import resolve_repository_scope
    from workspace_revision import verify_workspace_revision_unchanged

    return verify_workspace_revision_unchanged(resolve_repository_scope(root), revision)


def test_a_hidden_directory_is_outside_the_revision_as_it_is_outside_the_corpus(
    tmp_path,
):
    root = _repository(tmp_path / "repo", {"a.py": "x = 1\n"})
    (root / ".venv/lib").mkdir(parents=True)
    (root / ".venv/lib/vendored.py").write_text("y = 2\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/a.py").write_text("z = 3\n", encoding="utf-8")

    paths = [entry.path for entry in _revision(root).entries]

    assert paths == ["a.py"]


def test_a_nested_untracked_repository_does_not_make_every_verification_fail(tmp_path):
    """G-M8 point 1: git reports a directory the compute cannot record."""
    root = _repository(tmp_path / "repo", {"a.py": "x = 1\n"})
    _repository(root / "nested", {"b.py": "y = 2\n"})

    revision = _revision(root)

    assert (_verified(root, revision), _verified(root, revision)) == (True, True)


def test_a_file_git_calls_deleted_but_the_checkout_still_holds_is_hashed(tmp_path):
    """G-M8 point 2: `git rm --cached` of a file `.gitignore` also names."""
    root = _repository(tmp_path / "repo", {"a.py": "x = 1\n", ".gitignore": "cached.py\n"})
    (root / "cached.py").write_text("y = 2\n", encoding="utf-8")
    _git(root, "add", "-f", "--", "cached.py")
    _git(root, "commit", "-qm", "tracked and ignored")
    _git(root, "rm", "--cached", "-q", "--", "cached.py")

    revision = _revision(root)
    entry = next(item for item in revision.entries if item.path == "cached.py")

    assert (entry.kind, entry.sha256 is not None, _verified(root, revision)) == (
        "index-deleted",
        True,
        True,
    )


def test_a_change_to_a_file_git_calls_deleted_is_still_seen(tmp_path):
    root = _repository(tmp_path / "repo", {"a.py": "x = 1\n", ".gitignore": "cached.py\n"})
    (root / "cached.py").write_text("y = 2\n", encoding="utf-8")
    _git(root, "add", "-f", "--", "cached.py")
    _git(root, "commit", "-qm", "tracked and ignored")
    _git(root, "rm", "--cached", "-q", "--", "cached.py")
    revision = _revision(root)

    (root / "cached.py").write_text("y = 4\n", encoding="utf-8")

    assert _verified(root, revision) is False
