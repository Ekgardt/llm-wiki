"""The redrive measures when the fix reached this checkout, not when it was committed.

Audit 2026-09-26 B-28, docs/research/2026-09-26-a-fix-arrives-when-the-checkout-moves.md
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone

import scheduled_nightly


def _git(root, *arguments, env=None) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, env=env)


def test_a_commit_made_long_ago_arrives_when_head_moves(tmp_path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.test")
    _git(tmp_path, "config", "user.name", "t")
    old = {**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00"}
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "incoming")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix", env=old)
    _git(tmp_path, "checkout", "-q", "-")
    _git(tmp_path, "merge", "-q", "--ff-only", "incoming")

    arrived = datetime.fromisoformat(scheduled_nightly.head_arrival_time(tmp_path))

    assert datetime.now(timezone.utc) - arrived < timedelta(minutes=5)
