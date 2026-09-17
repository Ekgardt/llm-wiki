"""A dead nightly owner whose marker is gone or replaced can still be reclaimed.

Nightly and weekly share `run/maintenance.lock` but hold separate rows. The
marker is a projection, never the proof of death, so its absence must not
wedge the role for good.
See docs/research/2026-09-17-a-dead-owner-is-reclaimed-without-its-marker.md.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import markdown_transaction  # noqa: E402
import operational_ownership as ownership  # noqa: E402

MARKER = "run/maintenance.lock"


def _candidate(state_root: Path) -> Path:
    path = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return path


def _expire_owner(candidate: Path, lease: ownership.OwnerLease) -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace(
        "+00:00", "Z"
    )
    with contextlib.closing(sqlite3.connect(candidate)) as database:
        database.execute(
            "UPDATE maintenance_owners SET expires_at=? WHERE owner_token=?",
            (past, lease.token),
        )
        database.commit()


def _roles(candidate: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(candidate)) as database:
        rows = database.execute("SELECT role FROM maintenance_owners ORDER BY role")
        return [row[0] for row in rows]


def _crashed_nightly(state_root: Path) -> tuple[Path, ownership.OwnerLease]:
    """A nightly row whose lease lapsed and whose marker somebody removed."""
    candidate = _candidate(state_root)
    lease, _marker = ownership.acquire_scheduled_owner("nightly", state_root=state_root)
    _expire_owner(candidate, lease)
    (state_root / MARKER).unlink()
    return candidate, lease


def _registry_seeing_the_dead(state_root: Path) -> ownership.OwnershipRegistry:
    return ownership.OwnershipRegistry(state_root, process_probe=lambda _identity: "dead")


def test_a_dead_nightly_whose_marker_is_gone_is_reclaimed(tmp_path):
    state_root = tmp_path / "state"
    candidate, dead = _crashed_nightly(state_root)

    lease, marker = ownership.acquire_scheduled_owner(
        "nightly", state_root=state_root, registry=_registry_seeing_the_dead(state_root)
    )

    try:
        assert lease.epoch == dead.epoch + 1
        assert _roles(candidate) == ["nightly"]
        assert (state_root / MARKER).read_bytes() == str(os.getpid()).encode("ascii")
    finally:
        ownership.release_marker_owner(lease, marker)


def test_reclaiming_a_dead_nightly_leaves_a_live_weeklys_marker_alone(tmp_path):
    state_root = tmp_path / "state"
    candidate, _dead = _crashed_nightly(state_root)
    weekly, weekly_marker = ownership.acquire_scheduled_owner(
        "weekly", state_root=state_root
    )

    try:
        with pytest.raises(ownership.OperationalOwnershipError) as error:
            ownership.acquire_scheduled_owner(
                "nightly",
                state_root=state_root,
                registry=_registry_seeing_the_dead(state_root),
            )
        assert error.value.code == "owner_busy"
        assert _roles(candidate) == ["weekly"]
    finally:
        ownership.release_marker_owner(weekly, weekly_marker)
    assert not (state_root / MARKER).exists()
