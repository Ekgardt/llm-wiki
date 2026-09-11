"""Indexes that follow a repository's worktrees, and the opt-out mark (#24, D1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests import test_repository_index as _index_tests  # noqa: E402
from tests import test_repository_refresh as _refresh_tests  # noqa: E402
from tests.test_repository_index import ALPHA, _git, _repository  # noqa: E402

vault = _index_tests.vault
adopted_vault = _refresh_tests.adopted_vault
_isolated_reader_cache = _refresh_tests._isolated_reader_cache

LISTING = (
    "worktree /repo\0HEAD " + "a" * 40 + "\0branch refs/heads/main\0\0"
    "worktree /repo-topic\0HEAD " + "b" * 40 + "\0branch refs/heads/topic\0\0"
    "worktree /repo-detached\0HEAD " + "c" * 40 + "\0detached\0\0"
    "worktree /bare.git\0bare\0\0"
    "worktree /gone\0HEAD " + "d" * 40 + "\0detached\0prunable gitdir file points to non-existent location\0\0"
)


def test_the_porcelain_listing_is_read_record_by_record():
    from repository_worktrees import parse_worktrees

    parsed = parse_worktrees(LISTING)
    summary = [(item.path, item.branch, item.bare, item.prunable) for item in parsed]
    assert summary == [
        (Path("/repo"), "main", False, False),
        (Path("/repo-topic"), "topic", False, False),
        (Path("/repo-detached"), None, False, False),
        (Path("/bare.git"), None, True, False),
        (Path("/gone"), None, False, True),
    ]


def test_the_branch_key_decides_before_the_repository_key(tmp_path):
    from repository_worktrees import indexing_marked_off

    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    _git(repository, "checkout", "-q", "-b", "topic")
    unset = indexing_marked_off(repository)
    _git(repository, "config", "llmwiki.index", "false")
    repository_off = indexing_marked_off(repository)
    _git(repository, "config", "branch.topic.llmwikiIndex", "true")
    branch_on = indexing_marked_off(repository)
    _git(repository, "config", "branch.topic.llmwikiIndex", "false")
    assert (unset, repository_off, branch_on, indexing_marked_off(repository)) == (
        None,
        "llmwiki.index",
        None,
        "branch.topic.llmwikiIndex",
    )


def test_a_marked_checkout_is_refused_by_name_before_anything_is_built(vault, tmp_path):
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    _git(repository, "config", "llmwiki.index", "false")

    with pytest.raises(repository_index.RepositoryIndexRefused) as refused:
        repository_index.index_repository(repository, state_root=state)
    assert (refused.value.reason, refused.value.details["config_key"]) == (
        "repository_marked_not_indexed",
        "llmwiki.index",
    )
    assert repository_index.list_repositories(state_root=state)["repositories"] == []


def _worktree(repository: Path, name: str, branch: str) -> Path:
    path = repository.parent / name
    _git(repository, "worktree", "add", "-q", "-b", branch, str(path))
    return path


def test_a_new_worktree_of_a_registered_repository_is_indexed_by_the_timer(adopted_vault, tmp_path):
    import repository_index
    from repository_worktrees import unindexed_worktree_root

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    repository_index.index_repository(repository, state_root=state)
    topic = _worktree(repository, "repo-topic", "topic")
    before = unindexed_worktree_root(topic, state_root=state)

    answer = repository_index.refresh_all_repositories(state_root=state, budget_seconds=300)
    rows = repository_index.list_repositories(state_root=state)["repositories"]

    assert (before, [row["status"] for row in answer["followed"]]) == (topic.resolve(), ["followed"])
    assert sorted(Path(row["checkout_root"]).name for row in rows) == ["repo", "repo-topic"]
    assert unindexed_worktree_root(topic, state_root=state) is None


def test_a_marked_or_unregistered_worktree_is_not_followed(adopted_vault, tmp_path):
    import repository_index
    from repository_worktrees import follow_worktree, unindexed_worktree_root

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    with pytest.raises(repository_index.RepositoryIndexRefused) as unregistered:
        follow_worktree(repository, state_root=state)
    repository_index.index_repository(repository, state_root=state)
    topic = _worktree(repository, "repo-topic", "one-off")
    _git(topic, "config", "branch.one-off.llmwikiIndex", "false")

    followed = repository_index.refresh_all_repositories(state_root=state, budget_seconds=300)["followed"]
    assert (unregistered.value.reason, unindexed_worktree_root(topic, state_root=state)) == (
        "repository_not_registered",
        None,
    )
    assert [row.get("reason") for row in followed] == ["repository_marked_not_indexed"]


def test_the_first_answer_without_a_generation_starts_the_follow_once(vault, tmp_path, monkeypatch):
    import mcp_server
    import memory_state
    import repository_index

    _root, state = vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    repository_index.index_repository(repository, state_root=state)
    topic = _worktree(repository, "repo-topic", "topic")
    spawned = []
    monkeypatch.setattr(memory_state, "spawn_detached", lambda argv, **_kw: spawned.append(argv) or 4242)
    monkeypatch.setattr(mcp_server, "_FOLLOW_REQUESTED", set())

    first = mcp_server._worktree_follow_fields(topic)
    second = mcp_server._worktree_follow_fields(topic)
    assert (first["freshness"]["refresh"], second["freshness"]["refresh"]) == (
        "worktree_follow_started",
        "worktree_follow_already_requested",
    )
    assert [argv[-2:] for argv in spawned] == [["follow", str(topic.resolve())]]
    assert mcp_server._worktree_follow_fields(repository) == {}
