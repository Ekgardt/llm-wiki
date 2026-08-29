"""A worktree is merged when the branch that integrates work already has it.

`check_merged` asked only about `main`. On this machine every agent's work
lands on `work` and the owner merges `work` into `main` in batches, so nothing
had reached `main` since PR 12 and the question could not become true for
weeks. Measured 2026-08-29: thirteen worktrees were kept whose every commit
was already in `work`, 743 MB of them, and removing them by that evidence lost
no commit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cleanup_worktrees  # noqa: E402


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, capture_output=True, check=True)


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base")
    _git(repo, "branch", "work")
    return repo


def _info(branch: str) -> cleanup_worktrees.WorktreeInfo:
    return cleanup_worktrees.WorktreeInfo(
        path=Path("/nowhere"),
        branch=branch,
        is_main=False,
        is_clean=True,
        is_merged=False,
    )


def test_a_branch_already_in_the_integration_branch_counts_as_merged(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    _git(repo, "checkout", "-q", "work")
    _commit(repo, "landed")
    _git(repo, "branch", "topic", "HEAD")

    assert cleanup_worktrees.check_merged(_info("topic"), repo) is True


def test_a_branch_in_main_still_counts_as_merged(tmp_path: Path) -> None:
    """The original question keeps working; the new one is added, not swapped."""
    repo = _repository(tmp_path)
    _git(repo, "branch", "topic", "main")

    assert cleanup_worktrees.check_merged(_info("topic"), repo) is True


def test_a_branch_in_neither_is_not_merged(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, "unmerged")

    assert cleanup_worktrees.check_merged(_info("topic"), repo) is False


def test_a_detached_worktree_is_never_merged(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    assert cleanup_worktrees.check_merged(_info(""), repo) is False


def test_the_integration_branch_is_configurable(monkeypatch, tmp_path: Path) -> None:
    """A repository that integrates elsewhere is not forced into ours."""
    monkeypatch.setattr(cleanup_worktrees, "INTEGRATION_BRANCH", "nowhere")
    repo = _repository(tmp_path)
    _git(repo, "checkout", "-q", "work")
    _commit(repo, "landed")
    _git(repo, "branch", "topic", "HEAD")

    assert cleanup_worktrees.check_merged(_info("topic"), repo) is False
