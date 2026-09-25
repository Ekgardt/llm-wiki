"""The workspace revision holds only relevant files, nested server configuration included.

An edited `README.md` entered the revision while dirty and left it on commit, so
the delta reported it deleted; a nested `go.mod` or `Cargo.toml` never entered at
all. See docs/research/2026-09-25-a-revision-holds-only-what-it-proves.md.
"""

from __future__ import annotations

from pathlib import Path

from repository_scope import resolve_repository_scope
from workspace_revision import compute_workspace_revision

from tests.test_repository_index import _git, _repository


def _paths(root: Path) -> set[str]:
    return {entry.path for entry in compute_workspace_revision(resolve_repository_scope(root)).entries}


def test_a_dirty_readme_never_enters_and_a_nested_manifest_always_does(tmp_path: Path) -> None:
    root = _repository(
        tmp_path / "repo",
        {"README.md": "hello\n", "app.py": "x = 1\n", "sub/go.mod": "module sub\n", "crates/x/Cargo.toml": "[package]\n"},
    )
    (root / "README.md").write_text("hello again\n", encoding="utf-8")
    dirty = _paths(root)
    _git(root, "commit", "-qam", "readme")
    clean = _paths(root)

    assert ("README.md" in dirty, "README.md" in clean) == (False, False)
    assert {"sub/go.mod", "crates/x/Cargo.toml"} <= clean
