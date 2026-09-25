"""A snapshot that cannot commit says so, and one with nothing new still counts.

A failed `git commit` was reported as "no change", and an unchanged memory made no
commit, so doctor called a current copy stale. See
docs/research/2026-09-25-a-snapshot-says-when-it-last-looked.md.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import doctor
import pytest
import snapshot_knowledge


def _vault(tmp_path: Path) -> Path:
    notes = tmp_path / "vault" / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "alpha.md").write_text("tea", encoding="utf-8")
    return tmp_path / "vault"


@pytest.mark.skipif(sys.platform == "win32", reason="the refusing hook is a POSIX shell script")
def test_a_commit_that_fails_is_an_error_not_no_change(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    snapshot_knowledge.take_snapshot(_vault(tmp_path), root)
    (root / "knowledge" / "notes" / "beta.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "knowledge" / "notes" / "beta.md").write_text("coffee", encoding="utf-8")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n", encoding="utf-8")
    os.chmod(hook, 0o755)

    with pytest.raises(snapshot_knowledge.SnapshotFailed, match="git commit failed: refused"):
        snapshot_knowledge.take_snapshot(tmp_path / "vault", root)


def test_an_unchanged_memory_keeps_the_backup_current(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "snapshots"
    vault = _vault(tmp_path)
    snapshot_knowledge.take_snapshot(vault, root)
    monkeypatch.setenv("LLM_WIKI_SNAPSHOT_ROOT", str(root))
    later = datetime.now(timezone.utc) + timedelta(days=3)
    stale = doctor._backup_check(tmp_path, later)["status"]

    result = snapshot_knowledge.take_snapshot(vault, root)
    marker = snapshot_knowledge.last_checked_at(root)

    assert (stale, result["commit"], marker is not None) == ("degraded", "no change", True)
    assert doctor._backup_check(tmp_path, marker + timedelta(hours=1))["status"] == "ok"
