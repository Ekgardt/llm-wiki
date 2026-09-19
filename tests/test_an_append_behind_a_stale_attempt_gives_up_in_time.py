"""An append that cannot get past a stale `preparing` attempt stops at its deadline.

A writer that dies inside `prepare` leaves a `preparing` row. While the pid on it
answers as alive — its own, or one the system reused — nobody recovers it, the
attempt answers `retry`, and `retry` neither advanced nor was counted: the caller
looped for good, deadline or not.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import markdown_transaction
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT

_LOG = "knowledge/daily/2026-08-25.md"


class _Crash(BaseException):
    """Stops the writer the way a kill does: no handler tidies up after it."""


def _die_after_preparing(name: str, _parent: str | None = None) -> None:
    if name == "after_preparing":
        raise _Crash


def _append(coordinator: MarkdownCoordinator, outcome: list[object], **bounds) -> None:
    try:
        markdown_transaction._append_until_committed(
            coordinator, "op-1", _LOG, b"line\n", cancelled=None, **bounds
        )
    except (TimeoutError, _Crash) as error:
        outcome.append(type(error))


def _stale_attempt(tmp_path: Path) -> MarkdownCoordinator:
    """A coordinator whose first attempt at `op-1` died inside `prepare`."""
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    dying = MarkdownCoordinator(root, tmp_path / "state")
    dying._killpoint = _die_after_preparing  # type: ignore[method-assign]
    _append(dying, [], deadline=time.monotonic() + 30)
    return MarkdownCoordinator(root, tmp_path / "state")


def _states(coordinator: MarkdownCoordinator) -> list[str]:
    with sqlite3.connect(coordinator.database_path) as database:
        return [row[0] for row in database.execute('SELECT state FROM "transaction"')]


def test_the_caller_gets_a_timeout_at_its_deadline(tmp_path: Path) -> None:
    coordinator = _stale_attempt(tmp_path)
    outcome: list[object] = []
    worker = threading.Thread(
        target=_append,
        args=(coordinator, outcome),
        kwargs={"deadline": time.monotonic() + 1.0},
        daemon=True,
    )

    worker.start()
    worker.join(SHORT_TIMEOUT)

    assert _states(coordinator) == ["preparing"]
    assert (worker.is_alive(), outcome) == (False, [TimeoutError])


def test_without_a_deadline_the_caller_still_gets_a_timeout(tmp_path: Path) -> None:
    coordinator = _stale_attempt(tmp_path)
    outcome: list[object] = []
    worker = threading.Thread(
        target=_append,
        args=(coordinator, outcome),
        kwargs={"deadline": float("inf"), "stall_seconds": 0.5},
        daemon=True,
    )

    worker.start()
    worker.join(LONG_TIMEOUT)

    assert (worker.is_alive(), outcome) == (False, [TimeoutError])
