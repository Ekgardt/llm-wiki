"""Only a busy database means "nothing to claim yet"; any other database error is raised.

`_claim_when_reachable` answered `None` for every `sqlite3.OperationalError`, so a
disk I/O error or a missing table made the worker report an idle queue and exit 0.
"""

from __future__ import annotations

import sqlite3
import time

import memory_queue
import pytest


class _Queue:
    """A queue whose claim fails the way SQLite reports a given condition."""

    def __init__(self, message: str) -> None:
        self._message = message

    def claim(self, _owner: str, **_bounds: int):
        raise sqlite3.OperationalError(self._message)


def _claim(message: str):
    return memory_queue._claim_when_reachable(
        _Queue(message),
        owner="worker",
        lease_seconds=120,
        max_attempts=8,
        deadline=time.monotonic() + 0.2,
        monotonic=time.monotonic,
    )


def test_a_disk_error_reaches_the_operator() -> None:
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _claim("disk I/O error")


def test_a_busy_database_is_still_waited_out_and_then_idle() -> None:
    assert _claim("database is locked") is None
