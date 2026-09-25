"""A deadline that has passed starts no Git child for impact analysis (audit C-40).

docs/research/2026-09-25-an-impact-git-child-is-never-orphaned.md
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import impact_analysis  # noqa: E402


def _children() -> set[str]:
    tasks = Path("/proc/self/task")
    return {pid for task in tasks.iterdir() for pid in (task / "children").read_text().split()}


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="reads Linux /proc children")
def test_an_expired_deadline_leaves_no_git_child(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    before = _children()

    with pytest.raises(TimeoutError):
        impact_analysis._git(tmp_path, ["status"], deadline=time.monotonic() - 1, max_bytes=64)

    assert _children() - before == set()
