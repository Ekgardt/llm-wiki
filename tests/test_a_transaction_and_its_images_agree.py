"""No transaction directory without a row, no settled row waiting on a directory.

See docs/research/2026-09-25-a-transaction-and-its-images-agree.md.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from markdown_transaction import MarkdownCoordinator

from tests.test_every_store_has_a_bound import _append


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    (tmp_path / "vault/knowledge/daily").mkdir(parents=True)
    return MarkdownCoordinator(tmp_path / "vault", tmp_path / "state")


def _aged(path: Path, seconds: float) -> Path:
    path.mkdir(parents=True)
    moment = time.time() - seconds
    os.utime(path, (moment, moment))
    return path


def test_an_old_directory_no_row_names_is_removed_and_a_young_one_kept(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    _append(coordinator, "2026-01-01")
    old = _aged(coordinator.transaction_root / ("a" * 32), 7200)
    young = _aged(coordinator.transaction_root / ("b" * 32), 10)

    coordinator.prune()

    assert (old.exists(), young.exists()) == (False, True)


def test_a_settled_row_without_images_is_marked_pruned(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    _append(coordinator, "2026-01-01")
    for root in coordinator.transaction_root.iterdir():
        coordinator._remove_artifacts(root)

    coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3))

    with sqlite3.connect(coordinator.database_path) as database:
        marked = database.execute('SELECT artifacts_pruned_at IS NOT NULL FROM "transaction"').fetchall()
    assert marked == [(1,)]


def test_a_failed_insert_leaves_no_directory(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    root = coordinator.transaction_root / ("c" * 32)
    root.mkdir(parents=True)

    with pytest.raises(TimeoutError):
        coordinator._insert_preparing_row(
            "c" * 32, "post-tool:x", "d" * 64, {}, "2026-01-01T00:00:00Z", None, root, 0.0, None
        )

    assert not root.exists()
