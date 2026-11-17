"""A quarantine the rows show resolved gives up its images and keeps its row (audit 2026-09-27 B-2).

docs/research/2026-09-27-a-resolved-quarantine-lets-go-of-its-images.md
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.a_prune_keeps_what_resolves_a_quarantine_helpers import (
    _append,
    _quarantine,
    _unresolved,
    _vault,
)

SOON = timedelta(days=3)


def _row(coordinator, identifier: str) -> tuple:
    with sqlite3.connect(coordinator.database_path) as database:
        return database.execute(
            'SELECT state, artifacts_pruned_at IS NOT NULL FROM "transaction" WHERE id = ?', (identifier,)
        ).fetchone()


def test_a_resolved_quarantine_loses_its_images_and_keeps_its_row(tmp_path: Path) -> None:
    coordinator = _vault(tmp_path)
    refused = _append(coordinator, "post-tool:request", "2026-01-01")
    _quarantine(coordinator, refused)
    _append(coordinator, "post-tool:request:cas:1", "2026-01-01")

    coordinator.prune(now=datetime.now(UTC) + SOON)

    images = (coordinator.transaction_root / refused.id).exists()
    assert (images, _row(coordinator, refused.id), _unresolved(coordinator)) == (False, ("quarantined", 1), 0)


def test_an_unresolved_quarantine_keeps_its_images(tmp_path: Path) -> None:
    coordinator = _vault(tmp_path)
    lost = _append(coordinator, "post-tool:lost", "2026-01-02")
    _quarantine(coordinator, lost)

    coordinator.prune(now=datetime.now(UTC) + SOON)

    assert ((coordinator.transaction_root / lost.id).exists(), _row(coordinator, lost.id)) == (
        True, ("quarantined", 0)
    )
