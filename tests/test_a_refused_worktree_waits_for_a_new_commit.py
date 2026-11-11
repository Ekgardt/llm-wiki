"""A worktree refused at one commit takes no slot until it moves (audit 2026-09-26 B-12).

docs/research/2026-09-26-a-refused-worktree-waits-for-a-new-commit.md
"""
from __future__ import annotations

import shutil
from pathlib import Path

from tests.repository_index_helpers import ALPHA, _repository
from tests.repository_refresh_helpers import _isolated_reader_cache, adopted_vault  # noqa: F401
from tests.repository_worktrees_helpers import _worktree


def _refused_worktrees(repository: Path, count: int) -> None:
    """Worktrees whose code roots are gone: each is refused at its own HEAD."""
    for index in range(count):
        path = _worktree(repository, f"repo-broken-{index}", f"broken-{index}")
        shutil.rmtree(path / "pkg")


def test_refused_worktrees_do_not_hold_every_slot_twice(adopted_vault, tmp_path):  # noqa: F811
    import repository_index
    import repository_indexing
    from repository_worktrees import MAX_FOLLOWED_PER_PASS

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    repository_indexing.index_repository(repository, state_root=state)
    _refused_worktrees(repository, MAX_FOLLOWED_PER_PASS)
    wanted = _worktree(repository, "repo-wanted", "wanted")

    repository_index.refresh_all_repositories(state_root=state, budget_seconds=300)
    second = repository_index.refresh_all_repositories(state_root=state, budget_seconds=300)["followed"]

    assert [(Path(row["directory"]).name, row["status"]) for row in second] == [(wanted.name, "followed")]
