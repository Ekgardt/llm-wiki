"""A `preparing` row is kept only while the very process that wrote it still runs.

A writer that dies inside `prepare` leaves a `preparing` row. The row named its
writer by process number alone, and the operating system hands a number out again:
once another process held it, nobody recovered the row, every append behind it
stalled, and the doctor reported an unfinished transaction for good. The attempt
now records its writer's start identity beside its other evidence.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import markdown_transaction
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import LONG_TIMEOUT

_LOG = "knowledge/daily/2026-08-25.md"


class _Crash(BaseException):
    """Stops the writer the way a kill does: no handler tidies up after it."""


def _die_after_preparing(name: str, _parent: str | None = None) -> None:
    if name == "after_preparing":
        raise _Crash


def _append(coordinator: MarkdownCoordinator) -> str:
    try:
        record = markdown_transaction._append_until_committed(
            coordinator,
            "op-1",
            _LOG,
            b"line\n",
            cancelled=None,
            deadline=time.monotonic() + LONG_TIMEOUT,
            stall_seconds=1.0,
        )
    except (TimeoutError, _Crash) as error:
        return type(error).__name__
    return record.state


def _stale_attempt(tmp_path: Path) -> MarkdownCoordinator:
    """A coordinator whose first attempt at `op-1` died inside `prepare`."""
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    dying = MarkdownCoordinator(root, tmp_path / "state")
    dying._killpoint = _die_after_preparing  # type: ignore[method-assign]
    _append(dying)
    return MarkdownCoordinator(root, tmp_path / "state")


def _owner_record(coordinator: MarkdownCoordinator) -> Path:
    with sqlite3.connect(coordinator.database_path) as database:
        row = database.execute(
            """SELECT id FROM "transaction" WHERE state = 'preparing'"""
        ).fetchone()
    return coordinator.transaction_root / row[0] / "owner.json"


def _states(coordinator: MarkdownCoordinator) -> list[str]:
    with sqlite3.connect(coordinator.database_path) as database:
        rows = database.execute('SELECT state FROM "transaction" ORDER BY created_at')
        return [row[0] for row in rows]


def test_an_attempt_names_its_writer_by_number_and_start(tmp_path: Path) -> None:
    from operational_ownership import current_process_identity

    coordinator = _stale_attempt(tmp_path)

    recorded = json.loads(_owner_record(coordinator).read_bytes())

    identity = current_process_identity()
    assert recorded == {"pid": identity.pid, "start_identity": identity.start_identity}


def test_a_number_now_held_by_another_process_lets_the_attempt_go(
    tmp_path: Path,
) -> None:
    coordinator = _stale_attempt(tmp_path)
    another_start = json.dumps({"pid": os.getpid(), "start_identity": "another-start"})
    _owner_record(coordinator).write_bytes(another_start.encode("ascii"))

    outcome = _append(coordinator)

    assert (outcome, "preparing" in _states(coordinator)) == ("committed", False)


def test_the_living_writer_keeps_its_attempt(tmp_path: Path) -> None:
    coordinator = _stale_attempt(tmp_path)

    outcome = _append(coordinator)

    assert (outcome, _states(coordinator)) == ("TimeoutError", ["preparing"])


def test_a_row_older_than_the_record_is_judged_by_its_number(tmp_path: Path) -> None:
    coordinator = _stale_attempt(tmp_path)
    _owner_record(coordinator).unlink()

    outcome = _append(coordinator)

    assert (outcome, _states(coordinator)) == ("TimeoutError", ["preparing"])
