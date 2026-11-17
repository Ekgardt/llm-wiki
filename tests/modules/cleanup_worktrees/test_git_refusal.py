"""Native Git refusal is preserved; the existing two-step removal is not atomic."""
import cleanup_worktrees
import pytest

from tests.worktree_merge_is_asked_of_the_integration_branch_helpers import (
    _commit,
    _git,
    _repository,
)


def _integrated_topic(root):
    repo = _repository(root)
    _git(repo, 'checkout', '-q', 'work')
    _commit(repo, 'landed')
    _git(repo, 'branch', '--no-track', 'topic')
    _git(repo, 'checkout', '-q', 'main')
    path = repo / '.claude/worktrees/topic'
    _git(repo, 'worktree', 'add', str(path), 'topic')
    return repo, path


def test_git_refusal_preserves_branch_after_worktree_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_worktrees, 'MAIN_BRANCH', 'main')
    monkeypatch.setattr(cleanup_worktrees, 'INTEGRATION_BRANCH', 'work')
    repo, path = _integrated_topic(tmp_path)
    info = cleanup_worktrees.WorktreeInfo(path, 'topic', False, True, True)
    assert cleanup_worktrees.check_merged(info, repo)
    with pytest.raises(RuntimeError, match='git branch -d'):
        cleanup_worktrees.remove_worktree(info, repo)
    assert not path.exists()
    _git(repo, 'show-ref', '--verify', 'refs/heads/topic')
    _git(repo, 'show-ref', '--verify', 'refs/heads/work')
