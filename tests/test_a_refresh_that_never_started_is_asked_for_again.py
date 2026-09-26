"""Audit 3, B2: a failed spawn is not remembered as a request.

Research: `docs/research/2026-09-17-a-refresh-that-never-started-is-asked-for-again.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
import memory_state  # noqa: E402


@pytest.fixture
def spawn_results(monkeypatch, tmp_path):
    """The pids the next spawns return: None first (a failure), then a real one."""
    results = [None, 4242, 4243]
    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(memory_state, "spawn_detached", lambda _args, **_options: results.pop(0))
    monkeypatch.setattr(mcp_server, "_REFRESH_REQUESTED", {})
    monkeypatch.setattr(mcp_server, "_FOLLOW_REQUESTED", set())
    return results


def test_a_repository_refresh_is_retried_after_a_failed_spawn(spawn_results, tmp_path):
    checkout = SimpleNamespace(
        repository_id="repository:" + "a" * 64,
        checkout_id="checkout:" + "b" * 64,
        checkout_root=str(tmp_path),
        git_commit="c" * 40,
    )
    answers = [mcp_server._request_repository_refresh(checkout) for _ in range(3)]
    assert (answers, spawn_results) == (
        ["spawn_failed", "started", "already_requested"],
        [4243],
    )


def test_a_worktree_follow_is_retried_after_a_failed_spawn(spawn_results, tmp_path):
    answers = [mcp_server._request_worktree_follow(tmp_path) for _ in range(3)]
    assert (answers, spawn_results) == (
        ["spawn_failed", "worktree_follow_started", "worktree_follow_already_requested"],
        [4243],
    )
