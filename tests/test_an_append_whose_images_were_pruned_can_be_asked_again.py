"""An append asked again after its undo images were pruned writes, and does not raise.

Duplicate detection re-read the staged after-image. `prune` deletes that image
after the undo window and keeps the row, so the same permanent operation id —
a daily header, the vault log's header — raised `transaction after-image is
corrupt` on every later call, for good.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import LONG_TIMEOUT

_LOG = "knowledge/daily/2026-08-25.md"
_HEADER = b"# 2026-08-25\n"


def _append(coordinator: MarkdownCoordinator):
    return markdown_transaction._append_until_committed(
        coordinator,
        "daily-header:2026-08-25",
        _LOG,
        _HEADER,
        deadline=time.monotonic() + LONG_TIMEOUT,
        cancelled=None,
    )


def test_the_header_is_written_again_after_the_file_was_rotated_away(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    first = _append(coordinator)
    pruned = coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3))
    (root / _LOG).unlink()

    second = _append(coordinator)

    assert pruned == 1
    assert (second.state, second.id != first.id) == ("committed", True)
    assert (root / _LOG).read_bytes() == _HEADER


def _append_bytes(coordinator, operation_id, block):
    return markdown_transaction._append_until_committed(
        coordinator, operation_id, _LOG, block,
        deadline=time.monotonic() + LONG_TIMEOUT, cancelled=None,
    )


@pytest.mark.parametrize("before", [b"", b"earlier entry\n", b"a"])
def test_pruning_undo_images_does_not_repeat_an_append_that_is_still_present(tmp_path, before):
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    (root / _LOG).write_bytes(before)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    first = _append_bytes(coordinator, "accepted-event", b"aaa")
    _append_bytes(coordinator, "later-event", b"\nlater entry\n")
    expected = (root / _LOG).read_bytes()
    assert coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3)) == 2

    repeated = _append_bytes(coordinator, "accepted-event", b"aaa")

    assert repeated.id == first.id
    assert (root / _LOG).read_bytes() == expected


def test_a_pruned_create_is_not_repeated_while_its_original_file_remains(tmp_path):
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    first = _append(coordinator)
    coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3))

    repeated = _append(coordinator)

    assert repeated.id == first.id
    assert (root / _LOG).read_bytes() == _HEADER


def test_equal_text_from_distinct_operations_is_not_suppressed(tmp_path):
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    first = _append_bytes(coordinator, "first-event", b"same event text\n")
    second = _append_bytes(coordinator, "second-event", b"same event text\n")
    coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3))

    assert _append_bytes(coordinator, "first-event", b"same event text\n").id == first.id
    assert _append_bytes(coordinator, "second-event", b"same event text\n").id == second.id
    assert (root / _LOG).read_bytes() == b"same event text\n" * 2


def test_pruned_append_does_not_duplicate_text_when_prefix_evidence_conflicts(tmp_path):
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    target = root / _LOG
    target.write_bytes(b"original prefix\n")
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    _append_bytes(coordinator, "accepted-event", b"event text\n")
    coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3))
    changed = b"changed prefix\nevent text\n"
    target.write_bytes(changed)

    with pytest.raises(markdown_transaction.OperationBoundElsewhereError, match="hash evidence"):
        _append_bytes(coordinator, "accepted-event", b"event text\n")

    assert target.read_bytes() == changed
