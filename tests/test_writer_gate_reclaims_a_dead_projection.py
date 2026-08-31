"""A dead writer's gate row is reclaimed, not a permanent wedge.

`writer_owners.gate_name` is the primary key, so exactly one row can exist.
A nested gate records the project lease that entered it, and the registry
reclaims a dead owner by `(role, scope)` — which that row is keyed by neither.
Measured on the live vault 2026-08-28: one row for `global`, canonical owner
`project:fix-pip`, lease expired 20:50:21Z, pid 2095087 gone, and every
generation pass answered `sqlite3.IntegrityError: UNIQUE constraint failed:
writer_owners.gate_name` — twice in a row on an otherwise idle machine.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import operational_ownership as ownership  # noqa: E402

PROJECT_SCOPE = "project:wedged"


def _candidate(state_root: Path) -> Path:
    return state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"


def _coordinator(state_root: Path):
    markdown_transaction.initialize_coordinator_v3_candidate(
        _candidate(state_root), source_v2=None
    )
    return markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        _candidate(state_root), state_root=state_root
    )


def _dead_identity() -> ownership.ProcessIdentity:
    return ownership.ProcessIdentity(
        pid=os.getpid(), start_identity="llm-wiki-test:killed-process"
    )


def _expire(database_path: Path, table: str) -> None:
    when = datetime.now(timezone.utc) - timedelta(seconds=1)
    stamp = when.isoformat().replace("+00:00", "Z")
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        database.execute(f"UPDATE {table} SET expires_at=?", (stamp,))
        database.commit()


def _gate_rows(database_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        return int(
            database.execute("SELECT COUNT(*) FROM writer_owners").fetchone()[0]
        )


def _wedge(state_root: Path, monkeypatch: pytest.MonkeyPatch, *, dead: bool):
    """Leave a `global` gate row behind a project owner, as an abrupt death does."""
    coordinator = _coordinator(state_root)
    if dead:
        monkeypatch.setattr(ownership, "current_process_identity", _dead_identity)
    registry = ownership.OwnershipRegistry(state_root)
    owner = registry.acquire("project", scope=PROJECT_SCOPE)
    with contextlib.closing(sqlite3.connect(_candidate(state_root))) as database:
        markdown_transaction.MarkdownCoordinator._insert_writer_projection(
            database, owner
        )
        database.commit()
    if dead:
        monkeypatch.undo()
    return coordinator


def test_a_dead_writers_gate_row_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _wedge(tmp_path, monkeypatch, dead=True)
    _expire(_candidate(tmp_path), "writer_owners")
    _expire(_candidate(tmp_path), "maintenance_owners")

    with coordinator.writer_gate(wait_seconds=5) as lease:
        assert lease.role == "markdown-writer"


def test_the_gate_row_is_still_exactly_one_after_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _wedge(tmp_path, monkeypatch, dead=True)
    _expire(_candidate(tmp_path), "writer_owners")
    _expire(_candidate(tmp_path), "maintenance_owners")

    with coordinator.writer_gate(wait_seconds=5):
        assert _gate_rows(_candidate(tmp_path)) == 1


def test_a_live_holder_is_refused_by_name_not_by_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doubt refuses closed, and the wait window already knows `owner_busy`."""
    coordinator = _wedge(tmp_path, monkeypatch, dead=False)

    with pytest.raises(TimeoutError):
        with coordinator.writer_gate(wait_seconds=0.2):
            pass


def test_an_unexpired_dead_row_is_not_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the proof are required; a live lease is not ours to take."""
    coordinator = _wedge(tmp_path, monkeypatch, dead=True)

    with pytest.raises(TimeoutError):
        with coordinator.writer_gate(wait_seconds=0.2):
            pass
