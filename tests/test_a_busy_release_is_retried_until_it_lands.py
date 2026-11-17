"""A canonical release refused by a busy database is retried until it lands (audit 2026-09-26 A-13).

docs/research/2026-09-26-a-busy-release-is-retried-until-it-lands.md
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for entry in (str(TESTS.parent / "scripts"), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import markdown_transaction  # noqa: E402

from tests.markdown_coordinator_parts import patch_coordinator  # noqa: E402
from tests.slow_machine import LONG_TIMEOUT  # noqa: E402
from tests.sqlite_errors import busy_database_error  # noqa: E402
from tests.writer_gate_reclaims_a_dead_projection_helpers import (  # noqa: E402
    _candidate,
    _coordinator,
    _gate_rows,
)


def _locked_once(monkeypatch) -> None:
    real_delete = markdown_transaction.MarkdownCoordinator._delete_writer_projection
    calls = []

    def delete(database, owner):
        calls.append(owner)
        if len(calls) == 1:
            raise busy_database_error()
        return real_delete(database, owner)

    monkeypatch.setattr(markdown_transaction.MarkdownCoordinator, "_delete_writer_projection", staticmethod(delete))


def _join_release_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "markdown-writer-release":
            thread.join(timeout=LONG_TIMEOUT)


def test_a_busy_release_lands_later_and_the_next_writer_enters(tmp_path, monkeypatch) -> None:
    coordinator = _coordinator(tmp_path)
    patch_coordinator(monkeypatch, 'RELEASE_RETRY_DELAYS', (0.0, 0.0))
    _locked_once(monkeypatch)

    with coordinator.writer_gate():
        pass
    _join_release_threads()

    assert _gate_rows(_candidate(tmp_path)) == 0
    with coordinator.writer_gate(wait_seconds=0.5):
        pass
