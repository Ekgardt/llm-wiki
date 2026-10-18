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


def test_a_missing_table_named_locked_is_not_a_busy_queue():
    with sqlite3.connect(":memory:") as database:
        with pytest.raises(sqlite3.OperationalError) as caught:
            database.execute("SELECT * FROM locked")
    assert not memory_queue._is_busy_database(caught.value)


@pytest.mark.parametrize("message", ["no such table: locked", "no such table: busy"])
def test_legacy_error_text_does_not_retry_object_names(message):
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _claim(message)


@pytest.mark.parametrize("error", [ValueError("invalid lease"), PermissionError("denied"), sqlite3.OperationalError("disk I/O error")])
def test_maintenance_does_not_retry_unknown_or_permanent_errors(error):
    import doctor

    assert not doctor._transient_beat_failure(error)


@pytest.mark.parametrize("code, expected", [(5, True), (6, True), (261, True), (262, True), (10, False)])
def test_sqlite_primary_code_overrides_error_text(code, expected):
    import doctor
    from lease_renewal import busy_database

    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    classifiers = (busy_database, memory_queue._is_busy_database, doctor._transient_beat_failure)
    assert [classify(error) for classify in classifiers] == [expected] * len(classifiers)


def test_real_sqlite_contention_and_its_legacy_text_are_still_retryable(tmp_path):
    from contextlib import closing

    import doctor
    from lease_renewal import busy_database

    path = tmp_path / "busy.sqlite3"
    with closing(sqlite3.connect(path, timeout=0)) as first, closing(sqlite3.connect(path, timeout=0)) as second:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            second.execute("BEGIN IMMEDIATE")
    legacy = sqlite3.OperationalError(str(caught.value))
    for classify in (busy_database, memory_queue._is_busy_database, doctor._transient_beat_failure):
        assert classify(caught.value)
        assert classify(legacy)
