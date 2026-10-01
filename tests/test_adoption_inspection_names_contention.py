"""A real writer's lock is not evidence that adoption must be repeated."""

from __future__ import annotations

import contextlib
import multiprocessing
import sqlite3
import time

import doctor
import pytest
from installed_memory_repair import (
    _database_contention,
    inspect_installed_vault,
    repair_installed_vault,
)

from tests.slow_machine import LONG_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def _hold_database(path, channel):
    with contextlib.closing(sqlite3.connect(path)) as database:
        database.execute("BEGIN EXCLUSIVE")
        channel.send("locked")
        channel.recv()
        database.rollback()


def _finish_writer(process, channel):
    channel.send("release")
    process.join(LONG_TIMEOUT)
    if process.is_alive():
        process.kill()
        process.join(LONG_TIMEOUT)
    channel.close()
    assert process.exitcode == 0


@contextlib.contextmanager
def _locked_database(path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_database, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(LONG_TIMEOUT), "database writer did not become ready"
        assert parent.recv() == "locked"
        yield
    finally:
        _finish_writer(process, parent)


@pytest.mark.parametrize("database", ["queue-v3.sqlite3", "markdown-transactions-v3.sqlite3"])
def test_busy_adoption_stays_closed_without_prescribing_migration(tmp_path, database):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    with _locked_database(state / "run" / database):
        report = inspect_installed_vault(root=root, state_root=state)
        capture = doctor._capture_check(root, state, time.monotonic() + LONG_TIMEOUT)
        repair = repair_installed_vault(
            root=root, state_root=state,
            adopt_ownership_v3=True, confirm_all_agents_stopped=True,
        )
    assert report["overall_status"] == "error"
    assert report["details"]["adoption_state"] == "busy"
    assert report["blockers"] == [{"code": "operational_database_busy"}]
    assert capture["status"] == "degraded"
    assert "could not be checked" in capture["message"]
    assert "--apply" not in capture["message"]
    assert repair["overall_status"] == "error" and repair["actions"] == []
    assert inspect_installed_vault(root=root, state_root=state)["overall_status"] == "ok"


@pytest.mark.parametrize("code", [5, 6, 261, 517, 773, 262])
def test_extended_sqlite_contention_codes_are_recognized(code):
    error = sqlite3.OperationalError("localized or extended message")
    error.sqlite_errorcode = code
    assert _database_contention(error)


@pytest.mark.parametrize("error, expected", [
    (sqlite3.OperationalError("database is locked"), True),
    (sqlite3.OperationalError("database table is locked"), True),
    (sqlite3.OperationalError("database schema is locked"), True),
    (sqlite3.OperationalError("disk I/O error"), False),
    (ValueError("database is locked"), False),
])
def test_python_310_fallback_does_not_reclassify_other_failures(error, expected):
    assert _database_contention(error) is expected


def test_a_non_contention_code_takes_precedence_over_message_text():
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = 11
    assert not _database_contention(error)


def test_a_conflict_prescribes_inspection_before_offline_repair():
    message = doctor._capture_disabled_message("conflict")
    assert "--check" in message
    assert "--apply" not in message
