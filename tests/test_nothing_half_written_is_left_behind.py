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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
from markdown_transaction import MarkdownChange, MarkdownCoordinator  # noqa: E402

_PAGE = "knowledge/notes/a-page.md"


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return MarkdownCoordinator(root, tmp_path / "state")


def _committed(coordinator: MarkdownCoordinator) -> str:
    record = coordinator.prepare(
        [MarkdownChange.create(_PAGE, b"one\n")], operation_id="a-write"
    )
    coordinator.apply(record.id)
    return record.id


def _kill_a_prune(coordinator: MarkdownCoordinator, transaction_id: str) -> Path:
    """Exactly what `_prune_one` leaves when the process dies after its rename."""
    staged = coordinator.transaction_root / (
        f".{transaction_id}{markdown_transaction._STAGED_PRUNE_MARK}{uuid.uuid4().hex}"
    )
    (coordinator.transaction_root / transaction_id).replace(staged)
    return staged


def test_the_images_of_a_killed_prune_are_put_back_and_then_pruned(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    staged = _kill_a_prune(coordinator, transaction_id)
    later = datetime.now(timezone.utc) + timedelta(days=30)

    pruned = coordinator.prune(now=later)

    artifacts = coordinator.transaction_root / transaction_id
    assert (pruned, staged.exists(), artifacts.exists()) == (1, False, False)


def test_a_live_image_directory_wins_over_a_staged_copy(tmp_path: Path) -> None:
    """Both present means the rename was undone; the staged copy is the stale one."""
    coordinator = _coordinator(tmp_path)
    transaction_id = _committed(coordinator)
    staged = _kill_a_prune(coordinator, transaction_id)
    (coordinator.transaction_root / transaction_id).mkdir()

    coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=30))

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
