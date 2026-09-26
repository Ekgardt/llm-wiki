"""Restic leaves zero counters out of its summary; a clean restore must pass.

The summary below is the verbatim output of `restic 0.19.1`. The end-to-end
case runs only when that exact release is on PATH.
See docs/research/2026-09-17-restic-says-nothing-about-a-zero.md.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.slow_machine import SHORT_TIMEOUT
from tests.test_private_vault_backup import _restic_restore_fixture
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3

REAL_SUMMARY = (
    b'{"message_type":"summary","total_files":3,"files_restored":3,'
    b'"total_bytes":8,"bytes_restored":8}\n'
)


def _fake_restic(backup, snapshot: Path, target: Path, commands: list[list[str]]):
    def run(command, **_kwargs):
        commands.append(command)
        if command[-1] == "version":
            return backup.CommandResult(0, b"restic 0.19.1 fixture\n", b"")
        if command[-1] == "check":
            return backup.CommandResult(0, b"no errors were found\n", b"")
        shutil.copytree(snapshot, target, dirs_exist_ok=True, symlinks=True)
        return backup.CommandResult(0, REAL_SUMMARY, b"")

    return run


def test_a_summary_without_the_zero_counters_restores(tmp_path, monkeypatch):
    import private_vault_backup as backup

    _root, _state, snapshot, restic, repository_file, digest = _restic_restore_fixture(
        tmp_path
    )
    target = tmp_path / "restore"
    target.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        backup, "_run_bounded", _fake_restic(backup, snapshot, target, commands)
    )

    receipt = backup.restore_private_vault(
        target=target,
        restic_binary=restic,
        repository_file=repository_file,
        snapshot_id="b" * 64,
        expected_manifest_sha256=digest,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert receipt["manifest_sha256"] == digest
    assert (target / "vault/knowledge/notes/private.md").exists()
    assert "--quiet" in commands[-1]


def test_a_summary_that_names_a_skipped_file_is_still_refused():
    import private_vault_backup as backup

    output = REAL_SUMMARY.replace(b'"total_bytes"', b'"files_skipped":1,"total_bytes"')

    with pytest.raises(backup.BackupError) as raised:
        backup._restore_summary(output)

    assert raised.value.code == "restic_restore_incomplete"


def _real_restic() -> Path:
    found = shutil.which("restic")
    if found is None:
        pytest.skip("no restic on PATH")
    binary = Path(found).resolve()
    version = subprocess.run(
        [str(binary), "version"], capture_output=True, check=False, timeout=30
    ).stdout
    if not version.startswith(b"restic 0.19.1 "):
        pytest.skip("restic on PATH is not the pinned release")
    return binary


def test_a_real_restic_backs_up_and_restores_the_vault(tmp_path, monkeypatch):
    import private_vault_backup as backup

    binary = _real_restic()
    monkeypatch.setenv("RESTIC_PASSWORD", "fixture-only")
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "restic-cache"))
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"kept\n")
    repository = tmp_path / "repository"
    subprocess.run([str(binary), "-r", str(repository), "init", "-q"], check=True)
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text(f"{repository}\n", encoding="utf-8")
    staging, target = tmp_path / "staging", tmp_path / "restore"
    staging.mkdir()
    target.mkdir()

    saved = backup.backup_private_vault(
        root=root,
        state_root=state_root,
        staging_parent=staging,
        restic_binary=binary,
        repository_file=repository_file,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )
    receipt = backup.restore_private_vault(
        target=target,
        restic_binary=binary,
        repository_file=repository_file,
        snapshot_id=saved["snapshot_id"],
        expected_manifest_sha256=saved["manifest_sha256"],
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert receipt["manifest_sha256"] == saved["manifest_sha256"]
    assert (target / "vault/knowledge/notes/private.md").read_bytes() == b"kept\n"
