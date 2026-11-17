"""A backup takes what the hooks write as a snapshot, not a refusal (audit 2026-09-27 B-16).

docs/research/2026-09-27-a-backup-snapshots-what-hooks-write.md
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.reliability_v3_adoption_helpers import _vault, build_adopted_reliability_v3
from tests.slow_machine import SHORT_TIMEOUT

BEFORE = {"version": 1, "note": "before"}
AFTER = {"version": 2, "note": "after"}


def _hook_writes_during_copy(monkeypatch: pytest.MonkeyPatch, backup, trigger: Path, state_root: Path) -> None:
    """While the vault file is being copied, a hook rewrites state and files a new intent."""
    copy_regular_file = backup.copy_regular_file

    def copy_while_a_hook_writes(source: Path, destination: Path) -> None:
        copy_regular_file(source, destination)
        if source == trigger:
            (state_root / "run/state.json").write_text(json.dumps(AFTER), encoding="utf-8")
            intents = state_root / "run/capture-intents"
            intents.mkdir(exist_ok=True)
            (intents / "late.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(backup, "copy_regular_file", copy_while_a_hook_writes)


def test_a_state_write_during_the_copy_does_not_refuse_the_backup(tmp_path: Path, monkeypatch) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (state_root / "run/state.json").write_text(json.dumps(BEFORE), encoding="utf-8")
    note = root / "knowledge/notes/page.md"
    note.write_bytes(b"page\n")
    staging = tmp_path / "staging"
    staging.mkdir()
    _hook_writes_during_copy(monkeypatch, backup, note, state_root)

    with backup.staged_backup_image(
        root=root, state_root=state_root, staging_parent=staging, deadline=time.monotonic() + SHORT_TIMEOUT
    ) as image:
        copied = json.loads((image / "state/run/state.json").read_text(encoding="utf-8"))

    assert copied in (BEFORE, AFTER)


def test_the_hooks_lock_file_is_not_copied(tmp_path: Path) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (state_root / "run/state.json.lock").write_bytes(b"")
    staging = tmp_path / "staging"
    staging.mkdir()

    with backup.staged_backup_image(
        root=root, state_root=state_root, staging_parent=staging, deadline=time.monotonic() + SHORT_TIMEOUT
    ) as image:
        copied = (image / "state/run/state.json.lock").exists()

    assert copied is False
