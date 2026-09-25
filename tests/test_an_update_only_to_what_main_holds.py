"""The installed vault follows its remote's default branch, and only that branch.

Every push to `origin/work` reached the installed vault the next night, checked or
not (2026-09-14, `docs/research/2026-09-14-an-update-only-to-what-main-holds.md`);
the fix then followed the working branch up to what `main` held, which never
brought in what reached `main` from another branch (2026-09-24,
`docs/research/2026-09-24-the-vault-follows-its-default-branch.md`).
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
        "not_on_default_branch",
        False,
    )


def _merged_upstream(tmp_path: Path) -> tuple[Path, Path]:
    upstream, clone = _work_clone(tmp_path)
    _git(upstream, "checkout", "--quiet", "work")
    _commit(upstream, "checked.py", "value = 3\n")
    _git(upstream, "checkout", "--quiet", "main")
    _git(upstream, "merge", "--quiet", "--no-ff", "-m", "Merge pull request", "work")
    return upstream, clone


def test_a_checkout_on_the_default_branch_gets_the_merged_commit(tmp_path, monkeypatch):
    import self_update

    _upstream, clone = _merged_upstream(tmp_path)
    _git(clone, "checkout", "--quiet", "main")
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root, _extras: True)

    outcome = self_update.update_checkout(clone)

    assert (outcome["status"], (clone / "checked.py").exists()) == ("updated", True)


def test_a_checkout_left_on_a_working_branch_says_so_even_after_a_merge(tmp_path):
    import self_update

    _upstream, clone = _merged_upstream(tmp_path)

    outcome = self_update.update_checkout(clone)

    assert (outcome["status"], outcome["reason"], outcome["branch"]) == ("skipped", "not_on_default_branch", "work")


def test_a_checkout_ahead_of_its_remote_is_named_so(tmp_path):
    import self_update

    _upstream, clone = _work_clone(tmp_path)
    _git(clone, "checkout", "--quiet", "main")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _commit(clone, "local.py", "value = 5\n")

    outcome = self_update.update_checkout(clone)

    assert (outcome["status"], outcome["reason"]) == ("skipped", "ahead_of_remote")
