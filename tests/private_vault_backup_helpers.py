"""Helpers shared by test_private_vault_backup.py and the test files importing them."""
from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from tests.reliability_v3_adoption_helpers import _vault, build_adopted_reliability_v3
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


def _restic_restore_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, str, str]:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"restored private\n")
    backup_staging = tmp_path / "backup-staging"
    backup_staging.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=backup_staging,
        now=datetime(2026, 8, 15, tzinfo=UTC),
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        shutil.copytree(image, snapshot, dirs_exist_ok=True, symlinks=True)
    manifest_sha256 = backup.sha256_bytes((snapshot / "manifest.json").read_bytes())
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"fixture")
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text("s3:safe-bucket/private\n", encoding="utf-8")
    return root, state_root, snapshot, restic, repository_file, manifest_sha256


def _staged_image(tmp_path: Path):
    """One validated image, plus the digest a publish has to be given."""
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"private knowledge\n")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    kept = tmp_path / "image"
    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        now=datetime(2026, 8, 25, tzinfo=UTC),
        deadline=time.monotonic() + LONG_TIMEOUT,
    ) as image:
        shutil.copytree(image, kept)
    digest = backup.sha256_bytes((kept / "manifest.json").read_bytes())
    return kept, digest
