"""Helpers shared by test_operational_ownership.py and the test files importing them."""
from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import markdown_transaction
import operational_ownership as ownership
import pytest


@dataclass
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _candidate(state_root: Path) -> Path:
    return state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"


def _initialize_candidate(state_root: Path) -> Path:
    path = _candidate(state_root)
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return path


def _registry(
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: _Clock | None = None,
    process_probe=None,
) -> tuple[ownership.OwnershipRegistry, _Clock]:
    _initialize_candidate(state_root)
    current = ownership.ProcessIdentity(pid=31001, start_identity="test-process:1")
    monkeypatch.setattr(ownership, "current_process_identity", lambda: current)
    selected_clock = clock or _Clock(datetime(2026, 8, 12, 12, tzinfo=UTC))
    kwargs = {"clock": selected_clock}
    if process_probe is not None:
        kwargs["process_probe"] = process_probe
    return ownership.OwnershipRegistry(state_root, **kwargs), selected_clock


def _owner_count(state_root: Path) -> int:
    with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
        return int(database.execute("SELECT COUNT(*) FROM maintenance_owners").fetchone()[0])


def _expire(state_root: Path, lease: ownership.OwnerLease, when: datetime) -> None:
    with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
        database.execute(
            "UPDATE maintenance_owners SET expires_at=? WHERE role=? AND scope=?",
            (when.isoformat().replace("+00:00", "Z"), lease.role, lease.scope),
        )
        database.commit()
