"""The backup image holds the private vault, not the product Git already holds.

The scan left out only `cache/`, `logs/` and `run/`, so in the single-directory
layout the image carried `.git/`, `.venv/` and every tracked file. `publish`
writes only where the destination is absent or byte-identical, so publishing
into a fresh clone was refused on `vault/.git/config` — the last step of moving
memory to a new machine could not run (audit Q-M16). Research:
`docs/research/2026-09-17-a-backup-image-carries-what-git-does-not.md`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from reliable_memory import sha256_bytes

from tests.slow_machine import SHORT_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


def _committed_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A vault that is also a checkout, the way an installed one is."""
    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    (root / ".gitignore").write_text("knowledge/notes/\ncache/\nlogs/\nrun/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root, state_root


def _image_paths(backup, image: Path) -> set[str]:
    return {
        path.relative_to(image).as_posix()
        for path in image.rglob("*")
        if path.name != "manifest.json"
    }


@pytest.fixture
def image_paths(tmp_path: Path) -> set[str]:
    import private_vault_backup as backup

    root, state_root = _committed_vault(tmp_path)
    (root / "knowledge/notes/private.md").write_bytes(b"private\n")
    (root / "scripts/integration_adapter.py").write_bytes(b"locally modified\n")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        return _image_paths(backup, image)


def test_the_memory_and_the_modified_file_are_in_the_image(image_paths) -> None:
    assert "vault/knowledge/notes/private.md" in image_paths
    assert "vault/scripts/integration_adapter.py" in image_paths


def test_what_a_clone_already_has_is_not_in_the_image(image_paths) -> None:
    carried = {path for path in image_paths if path.startswith("vault/.git")}

    assert (carried, "vault/.gitignore" in image_paths) == (set(), False)
    assert "vault/knowledge/index.md" not in image_paths


def test_a_directory_of_only_carried_files_is_not_in_the_image(
    tmp_path: Path,
) -> None:
    import private_vault_backup as backup

    root, state_root = _committed_vault(tmp_path)
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()

    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        paths = _image_paths(backup, image)

    assert "vault/scripts" not in paths


def test_a_vault_that_is_no_repository_keeps_the_whole_image(tmp_path: Path) -> None:
    """Without git to ask, nothing is left out on git's word."""
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"private\n")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()

    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        paths = _image_paths(backup, image)

    assert "vault/scripts/integration_adapter.py" in paths
    assert "vault/knowledge/index.md" in paths


def test_a_regenerable_directory_is_never_in_the_image(tmp_path: Path) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    for name in ("__pycache__", ".venv", "node_modules"):
        (root / name).mkdir()
        (root / name / "artifact.bin").write_bytes(b"regenerable\n")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()

    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        paths = _image_paths(backup, image)

    assert not [path for path in paths if "artifact.bin" in path]


def test_the_image_publishes_into_a_fresh_clone(tmp_path: Path) -> None:
    """The step that used to be refused on `vault/.git/config`."""
    import private_vault_backup as backup

    root, state_root = _committed_vault(tmp_path)
    (root / "knowledge/notes/private.md").write_bytes(b"private\n")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(root), str(clone)], check=True, capture_output=True
    )
    fresh_state = tmp_path / "clone-state"
    (fresh_state / "run").mkdir(parents=True)

    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) as image:
        backup.validate_backup_image(image)
        manifest_sha256 = sha256_bytes((image / "manifest.json").read_bytes())
        receipt = backup.publish_restored_image(
            image=image,
            vault_root=clone,
            state_root=fresh_state,
            expected_manifest_sha256=manifest_sha256,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

    assert (clone / "knowledge/notes/private.md").read_bytes() == b"private\n"
    assert receipt["published_files"] >= 3


if __name__ == "__main__":  # pragma: no cover - direct run convenience
    sys.exit(pytest.main([__file__]))
