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
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import SHORT_TIMEOUT

_LOG = "knowledge/daily/2026-08-25.md"
_HEADER = b"# 2026-08-25\n"


def _append(coordinator: MarkdownCoordinator):
    return markdown_transaction._append_until_committed(
        coordinator,
        "daily-header:2026-08-25",
        _LOG,
        _HEADER,
        deadline=time.monotonic() + SHORT_TIMEOUT,
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
