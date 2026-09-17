"""The deletion check and the backup wait for live owners, not for dead ones.

See the addendum of
docs/research/2026-09-17-a-dead-owner-is-reclaimed-without-its-marker.md.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import markdown_transaction  # noqa: E402
import operational_ownership as ownership  # noqa: E402

START = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def _registry(state_root: Path, *, at: datetime, probe: str) -> ownership.OwnershipRegistry:
    return ownership.OwnershipRegistry(
        state_root, clock=lambda: at, process_probe=lambda _identity: probe
    )


def _roles(state_root: Path) -> list[str]:
    path = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    with contextlib.closing(sqlite3.connect(path)) as database:
        rows = database.execute("SELECT role FROM maintenance_owners ORDER BY role")
        return [row[0] for row in rows]


def _abandoned_worker(state_root: Path) -> None:
    """A worker scope nobody will ever acquire again, left behind by a crash."""
    markdown_transaction.initialize_coordinator_v3_candidate(
        state_root / "run/markdown-transactions-v3.candidate.sqlite3", source_v2=None
    )
    _registry(state_root, at=START, probe="alive").acquire(
        "queue-worker", scope="worker:once", actor_id="crashed-worker"
    )


def test_a_provably_dead_owner_is_reclaimed_by_the_quiescence_check(tmp_path):
    state_root = tmp_path / "state"
    _abandoned_worker(state_root)
    later = _registry(state_root, at=START + timedelta(hours=1), probe="dead")

    lease = later.acquire(
        "runtime-deletion-check", scope="backup-snapshot", actor_id="backup"
    )

    assert _roles(state_root) == ["runtime-deletion-check"]
    later.release(lease)


def test_an_expired_owner_whose_process_lives_still_refuses(tmp_path):
    state_root = tmp_path / "state"
    _abandoned_worker(state_root)
    later = _registry(state_root, at=START + timedelta(hours=1), probe="alive")

    with pytest.raises(ownership.OperationalOwnershipError) as refused:
        later.acquire("runtime-deletion-check", scope="backup-snapshot", actor_id="backup")

    assert refused.value.code == "runtime_deletion_check_requires_quiescence"
    assert _roles(state_root) == ["queue-worker"]
