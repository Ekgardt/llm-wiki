"""Filesystem helpers the tests share.

`remove_tree` exists because a Git checkout holds read-only object files, and
on Windows `shutil.rmtree` refuses to unlink a read-only file (`WinError 5`).
The Python `shutil` reference's own example clears the bit and retries; this
clears it up front so the removal has one shape on every platform. Research:
`docs/research/2026-09-10-a-cached-reader-needs-one-thread-at-a-time-not-a-serialized-build.md`.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path


def remove_tree(root: Path) -> None:
    """Remove `root` and everything under it, read-only entries included."""
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
    shutil.rmtree(root)
