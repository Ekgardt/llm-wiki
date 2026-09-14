"""A restored image refused by a populated vault writes nothing, and a failure part-way undoes itself.

The conflict was found at its own file's turn, after every earlier file was already
written. Research: `docs/research/2026-09-14-a-publication-is-all-or-nothing.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_private_vault_backup import _staged_image  # noqa: E402


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_a_conflict_anywhere_writes_no_file_at_all(tmp_path):
    import private_vault_backup as backup

    image, digest = _staged_image(tmp_path)
    target = tmp_path / "new-machine"
    (target / "run").mkdir(parents=True)
    last = max((image / "vault").rglob("*.md"))
    clash = target / last.relative_to(image / "vault")
    clash.parent.mkdir(parents=True, exist_ok=True)
    clash.write_bytes(b"someone else's work\n")

    with pytest.raises(backup.BackupError):
        backup.publish_restored_image(image=image, vault_root=target, state_root=target, expected_manifest_sha256=digest)

    assert _files(target) == [clash.relative_to(target).as_posix()]


def test_a_failure_part_way_removes_what_was_written(tmp_path, monkeypatch):
    import private_vault_backup as backup

    image, digest = _staged_image(tmp_path)
    target = tmp_path / "new-machine"
    (target / "run").mkdir(parents=True)
    real_write = backup._write_new
    calls: list[int] = []

    def fail_second(source, destination):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        real_write(source, destination)

    monkeypatch.setattr(backup, "_write_new", fail_second)

    with pytest.raises(OSError):
        backup.publish_restored_image(image=image, vault_root=target, state_root=target, expected_manifest_sha256=digest)

    assert _files(target) == []
