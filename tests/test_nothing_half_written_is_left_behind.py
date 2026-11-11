"""A prune that died mid-rename, and a Windows publish that failed, leave nothing.

`_prune_one` stages a transaction's images aside as `.<id>.pruning-<uuid>` and
puts them back for an exception — but a process that dies leaves the directory
where nothing ever looks again. `_publish_windows_target` removed its
`.<name>.<uuid>.tmp` only when the publish said "duplicate", so a failed write
left debris beside the page, inside `knowledge/`.

See `docs/research/2026-09-18-nothing-half-written-is-left-behind.md`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from tests.nothing_half_written_is_left_behind_helpers import (  # noqa: E402
    _PAGE,
    _committed,
    _coordinator,
    _kill_a_prune,
)


def test_the_images_of_a_killed_prune_are_put_back_and_then_pruned(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    staged = _kill_a_prune(coordinator, transaction_id)
    later = datetime.now(UTC) + timedelta(days=30)

    pruned = coordinator.prune(now=later)

    artifacts = coordinator.transaction_root / transaction_id
    assert (pruned, staged.exists(), artifacts.exists()) == (1, False, False)


def test_a_live_image_directory_wins_over_a_staged_copy(tmp_path: Path) -> None:
    """Both present means the rename was undone; the staged copy is the stale one."""
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    staged = _kill_a_prune(coordinator, transaction_id)
    (coordinator.transaction_root / transaction_id).mkdir()

    coordinator.prune(now=datetime.now(UTC) + timedelta(days=30))

    assert not staged.exists()


def test_a_failed_windows_publish_leaves_no_temporary_beside_the_page(
    tmp_path: Path,
) -> None:
    """The POSIX path already cleans up in a `finally`; this is its Windows twin."""
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / _PAGE
    target.write_bytes(b"one\n")
    row = {"kind": "replace", "after_hash": "0" * 64, "path": _PAGE}

    with pytest.raises(Exception):  # noqa: B017, PT011 - any failure, the debris is the point
        coordinator._publish_windows_target(row, target, b"two\n")

    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
