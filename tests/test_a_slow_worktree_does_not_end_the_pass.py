"""A Git call that outlives its bound in the worktree helpers is a named refusal.

`subprocess.TimeoutExpired` is not a `TimeoutError`, so no deferral caught it and
one slow checkout ended the nightly pass. See
docs/research/2026-09-25-a-slow-worktree-does-not-end-the-pass.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import repository_index
import repository_worktrees


def test_a_timed_out_git_read_is_a_repository_refusal(tmp_path: Path, monkeypatch) -> None:
    def slow(*_args, **_options):
        raise subprocess.TimeoutExpired(["git"], 1)

    monkeypatch.setattr(repository_index.subprocess, "run", slow)

    with pytest.raises(repository_index.RepositoryIndexRefused) as refused:
        repository_worktrees.require_indexing_wanted(tmp_path)

    assert refused.value.as_dict()["reason"] == "repository_git_probe_timed_out"
