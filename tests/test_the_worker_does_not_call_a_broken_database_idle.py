"""Only a busy database means "nothing to claim yet"; any other database error is raised.

`claim_when_reachable` answered `None` for every `sqlite3.OperationalError`, so a
disk I/O error or a missing table made the worker report an idle queue and exit 0.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
import queue_core

from tests.sqlite_errors import busy_database_error


class _Queue:
    """A queue whose claim fails the way SQLite reports a given condition."""

    def __init__(self, error: sqlite3.OperationalError) -> None:
        self._error = error

    def claim(self, _owner: str, **_bounds: int):
        raise self._error


def _claim(error: sqlite3.OperationalError):
    return queue_core.claim_when_reachable(
        _Queue(error),
        owner="worker",
        lease_seconds=120,
        max_attempts=8,
        deadline=time.monotonic() + 0.2,
        monotonic=time.monotonic,
    )


def test_a_disk_error_reaches_the_operator() -> None:
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _claim(sqlite3.OperationalError("disk I/O error"))


def test_a_busy_database_is_still_waited_out_and_then_idle() -> None:
    assert _claim(busy_database_error()) is None


def test_a_missing_table_named_locked_is_not_a_busy_queue():
    with sqlite3.connect(":memory:") as database:
        with pytest.raises(sqlite3.OperationalError) as caught:
            database.execute("SELECT * FROM locked")
    assert not queue_core.is_busy_database(caught.value)


@pytest.mark.parametrize("message", ["no such table: locked", "no such table: busy"])
def test_legacy_error_text_does_not_retry_object_names(message):
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _claim(sqlite3.OperationalError(message))


@pytest.mark.parametrize("error", [ValueError("invalid lease"), PermissionError("denied"), sqlite3.OperationalError("disk I/O error")])
def test_maintenance_does_not_retry_unknown_or_permanent_errors(error):
    import generation_maintenance

    assert not generation_maintenance.transient_beat_failure(error)


@pytest.mark.parametrize("code, expected", [(5, True), (6, True), (261, True), (262, True), (10, False)])
def test_sqlite_primary_code_overrides_error_text(code, expected):
    import generation_maintenance
    from lease_renewal import busy_database

    error = busy_database_error()
    error.sqlite_errorcode = code
    classifiers = (busy_database, queue_core.is_busy_database, generation_maintenance.transient_beat_failure)
    assert [classify(error) for classify in classifiers] == [expected] * len(classifiers)


def test_real_sqlite_contention_is_still_retryable(tmp_path):
    from contextlib import closing

    import generation_maintenance
    from lease_renewal import busy_database

    path = tmp_path / "busy.sqlite3"
    with closing(sqlite3.connect(path, timeout=0)) as first, closing(sqlite3.connect(path, timeout=0)) as second:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            second.execute("BEGIN IMMEDIATE")
    for classify in (busy_database, queue_core.is_busy_database, generation_maintenance.transient_beat_failure):
        assert classify(caught.value)
