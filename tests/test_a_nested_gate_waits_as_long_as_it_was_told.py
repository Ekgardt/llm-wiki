"""A writer gate entered with an owner waits for a live writer, as the plain gate does.

`writer_gate(owner=..., wait_seconds=...)` ignored its wait: one attempt, then
`owner_busy`. A project checkpoint that met a running compile failed at once,
although its caller had asked to wait. Research:
`docs/research/2026-09-17-a-transaction-that-is-over-stays-over.md`.
"""

from __future__ import annotations

import sys
import threading
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
)


def _hold_the_gate(coordinator, held: threading.Event, release: threading.Event) -> None:
    with coordinator.writer_gate():
        held.set()
        release.wait(30)


def _live_writer(state_root: Path) -> tuple[threading.Thread, threading.Event]:
    """Another thread inside the plain gate, until `release` is set."""
    held, release = threading.Event(), threading.Event()
    second = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        _candidate(state_root), state_root=state_root
    )
    writer = threading.Thread(
        target=_hold_the_gate, args=(second, held, release), daemon=True
    )
    writer.start()
    held.wait(30)
    return writer, release


def test_the_owner_enters_once_the_live_writer_leaves(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    owner = ownership.OwnershipRegistry(tmp_path).acquire("project", scope=PROJECT_SCOPE)
    writer, release = _live_writer(tmp_path)
    threading.Timer(0.5, release.set).start()

    with coordinator.writer_gate(owner=owner, wait_seconds=30):
        entered = coordinator.writer_gate_held()

    writer.join(30)
    assert entered is True


def test_the_refusal_is_still_owner_busy_when_the_wait_runs_out(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    owner = ownership.OwnershipRegistry(tmp_path).acquire("project", scope=PROJECT_SCOPE)
    writer, release = _live_writer(tmp_path)

    with pytest.raises(ownership.OperationalOwnershipError) as refusal:
        with coordinator.writer_gate(owner=owner, wait_seconds=0.2):
            pass

    release.set()
    writer.join(30)
    assert refusal.value.code == "owner_busy"
