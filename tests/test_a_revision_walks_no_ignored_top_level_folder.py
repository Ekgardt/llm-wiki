"""A top-level folder git ignores whole is not walked by the workspace revision.

A TypeScript checkout with its dependencies installed made the walk examine
`node_modules` for 14.8 s and then refuse at the 100 000-entry ceiling. See
docs/research/2026-09-25-a-revision-walks-no-ignored-top-level-folder.md.
"""

from __future__ import annotations

from pathlib import Path

import workspace_revision
from repository_scope import resolve_repository_scope

from tests.test_repository_index import _repository


def test_an_ignored_dependency_tree_does_not_reach_the_ceiling(tmp_path: Path, monkeypatch) -> None:
    root = _repository(tmp_path / "repo", {"src/app.ts": "export const a = 1;\n", ".gitignore": "node_modules/\n"})
    for index in range(40):
        package = root / "node_modules" / f"dep{index}"
        package.mkdir(parents=True)
        (package / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    monkeypatch.setattr(workspace_revision, "MAX_REVISION_FILES", 20)

    revision = workspace_revision.compute_workspace_revision(resolve_repository_scope(root))

    paths = [entry.path for entry in revision.entries]
    assert ("src/app.ts" in paths, [path for path in paths if path.startswith("node_modules/")]) == (True, [])


def test_the_ignored_set_comes_from_git_at_any_depth(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo", {"src/app.ts": "x\n", ".gitignore": "dist/\nbuild/\n"})
    (root / "dist").mkdir()
    (root / "dist" / "out.js").write_text("x\n", encoding="utf-8")
    (root / "src" / "build").mkdir()
    (root / "src" / "build" / "x.js").write_text("x\n", encoding="utf-8")

    ignored = workspace_revision.ignored_directories(root, deadline=None, cancelled=None)

    assert ignored == frozenset({"dist", "src/build"})
