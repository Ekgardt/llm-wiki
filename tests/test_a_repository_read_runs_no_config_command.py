"""Reading a repository runs no program its own config names.

`core.fsmonitor` in a repository's `.git/config` ran on `ls-files` and `status` from
the indexer and the worktree cleaner. Research:
`docs/research/2026-09-14-a-repository-read-runs-no-config-command.md`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the hook program is a POSIX shell script")


def _hostile_repository(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    repo.mkdir()
    for arguments in (["init", "-q"], ["config", "core.fsmonitor", str(hook)]):
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)
    (repo / "file.txt").write_text("x\n", encoding="utf-8")
    return repo, marker


def test_the_hostile_fixture_does_run_its_hook_for_plain_git(tmp_path):
    repo, marker = _hostile_repository(tmp_path)

    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, check=False)

    assert marker.exists()


def test_the_indexer_and_the_cleaner_do_not_run_it(tmp_path):
    import cleanup_worktrees
    import repository_index

    repo, marker = _hostile_repository(tmp_path)

    repository_index._git_text(repo, "ls-files", "-z")
    cleanup_worktrees._run(["git", "status", "--porcelain"], cwd=repo)

    assert not marker.exists()
