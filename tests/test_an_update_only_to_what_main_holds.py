"""A checkout on a working branch is updated only to commits its remote's default branch holds.

Every push to `origin/work` reached the installed vault the next night, checked or not.
Research: `docs/research/2026-09-14-an-update-only-to-what-main-holds.md`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_self_update import _commit, _git, _repository  # noqa: E402


def _work_clone(tmp_path: Path) -> tuple[Path, Path]:
    upstream = _repository(tmp_path / "upstream")
    _commit(upstream, "product.py", "value = 1\n")
    _git(upstream, "branch", "work")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", "--branch", "work", str(upstream), str(clone)], check=True, capture_output=True)
    return upstream, clone


def test_a_pushed_but_unmerged_commit_is_not_installed(tmp_path):
    import self_update

    upstream, clone = _work_clone(tmp_path)
    _git(upstream, "checkout", "--quiet", "work")
    _commit(upstream, "unchecked.py", "value = 2\n")

    outcome = self_update.update_checkout(clone)

    assert (outcome["status"], outcome["reason"], (clone / "unchecked.py").exists()) == (
        "skipped",
        "not_in_default_branch",
        False,
    )


def test_the_same_commit_is_installed_once_it_is_merged(tmp_path, monkeypatch):
    import self_update

    upstream, clone = _work_clone(tmp_path)
    _git(upstream, "checkout", "--quiet", "work")
    _commit(upstream, "checked.py", "value = 3\n")
    _git(upstream, "checkout", "--quiet", "main")
    _git(upstream, "merge", "--quiet", "--no-ff", "-m", "Merge pull request", "work")
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root: True)

    outcome = self_update.update_checkout(clone)

    assert (outcome["status"], (clone / "checked.py").exists()) == ("updated", True)
