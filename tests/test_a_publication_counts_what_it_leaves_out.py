"""A publication writes regular files only, and its receipt says what it left out.

See the addendum of docs/research/2026-09-17-a-refused-publication-names-its-file.md.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def _image_with_a_link_and_an_empty_directory(tmp_path: Path) -> tuple[Path, str, int]:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"private knowledge\n")
    (root / "knowledge/empty-shelf").mkdir()
    try:
        os.symlink("private.md", root / "knowledge/notes/alias.md")
    except OSError:
        pytest.skip("symlink creation unavailable")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    kept = tmp_path / "image"
    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        now=datetime(2026, 8, 25, tzinfo=timezone.utc),
        deadline=time.monotonic() + 60,
    ) as image:
        shutil.copytree(image, kept, symlinks=True)
    return kept, backup.sha256_bytes((kept / "manifest.json").read_bytes()), _left_out(kept)


def _links(image: Path) -> list[Path]:
    return [path for path in image.rglob("*") if path.is_symlink()]


def _empty_directories(image: Path) -> list[str]:
    return [directory for directory, names, files in os.walk(image) if not names + files]


def _left_out(image: Path) -> int:
    """Counted another way: every link, and every directory with nothing in it."""
    return len(_links(image)) + len(_empty_directories(image))


def test_the_receipt_counts_the_link_and_the_image_holds_no_empty_directory(tmp_path):
    """The one entry a publication leaves out of a fresh image is the symlink.

    An empty directory never reaches the image: "Directories that end up holding
    nothing are not part of the image, so the image is the shape of what it
    carries" —
    `docs/research/2026-09-17-a-backup-image-carries-what-git-does-not.md`. The
    receipt still counts both kinds, because an image restored from an older
    snapshot may hold either.
    """
    import private_vault_backup as backup

    image, digest, left_out = _image_with_a_link_and_an_empty_directory(tmp_path)
    target = tmp_path / "new-machine"
    (target / "run").mkdir(parents=True)

    receipt = backup.publish_restored_image(
        image=image, vault_root=target, state_root=target, expected_manifest_sha256=digest
    )

    by_kind = (len(_links(image)), len(_empty_directories(image)))
    assert (
        receipt["unpublished_entries"],
        left_out,
        by_kind,
        (target / "knowledge/notes/alias.md").exists(),
    ) == (1, 1, (1, 0), False)
