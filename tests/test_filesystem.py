"""`remove_tree` removes a checkout whose object files are read-only."""

from __future__ import annotations

import stat
from pathlib import Path

from tests.filesystem import remove_tree


def test_a_tree_with_a_read_only_file_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    objects = root / ".git" / "objects" / "0b"
    objects.mkdir(parents=True)
    blob = objects / "f7a0"
    blob.write_bytes(b"x")
    blob.chmod(stat.S_IREAD)

    remove_tree(root)

    assert not root.exists()
