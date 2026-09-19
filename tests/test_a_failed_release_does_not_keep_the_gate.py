"""A nested gate whose release failed is left neither in the thread nor in the table.

The delete of the projection row can fail with `database is locked`. The thread used to keep
`gate_depth` at 1 and the row shut every other writer out until the process exited. Research:
`docs/research/2026-09-17-a-failed-release-does-not-keep-the-gate.md`.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS.parent / "scripts"
for entry in (str(SCRIPTS_DIR), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import markdown_transaction  # noqa: E402
import operational_ownership as ownership  # noqa: E402
from test_writer_gate_reclaims_a_dead_projection import (  # noqa: E402
    PROJECT_SCOPE,
    _candidate,
    _coordinator,
    _gate_rows,
)


def _locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


def _failed_release(state_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Enter the nested gate and let the release's delete fail once."""
    coordinator = _coordinator(state_root)
    owner = ownership.OwnershipRegistry(state_root).acquire("project", scope=PROJECT_SCOPE)
    with monkeypatch.context() as patch, pytest.raises(sqlite3.OperationalError):
        with coordinator.writer_gate(owner=owner):
            patch.setattr(markdown_transaction.MarkdownCoordinator, "_delete_writer_projection", _locked)
    return coordinator, owner


def test_the_thread_is_outside_the_gate_after_a_failed_release(tmp_path, monkeypatch) -> None:
    coordinator, _owner = _failed_release(tmp_path, monkeypatch)

    assert coordinator.writer_gate_held() is False


def test_the_same_lease_enters_again_and_leaves_no_row(tmp_path, monkeypatch) -> None:
    coordinator, owner = _failed_release(tmp_path, monkeypatch)

    with coordinator.writer_gate(owner=owner):
        pass

    assert _gate_rows(_candidate(tmp_path)) == 0


def test_a_row_without_a_recorded_failure_is_not_removed(tmp_path, monkeypatch) -> None:
    """A row that only looks like ours may be a live gate; it is judged by the registry."""
    coordinator, owner = _failed_release(tmp_path, monkeypatch)
    markdown_transaction._UNRELEASED_PROJECTIONS.clear()

    with pytest.raises((TimeoutError, RuntimeError, sqlite3.IntegrityError)):
        with coordinator.writer_gate(owner=owner, wait_seconds=0.2):
            pass
