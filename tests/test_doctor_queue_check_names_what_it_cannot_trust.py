from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from llm_wiki import doctor_queue, monotonic_deadline
from llm_wiki.doctor_base import MAX_OPERATIONAL_ROWS

TASK_COLUMNS = (
    "id TEXT PRIMARY KEY, state TEXT, error_code TEXT, blocked_capability TEXT, "
    "result_reference TEXT, result_sha256 TEXT, redrive_of TEXT, updated_at TEXT"
)
NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
posix_permissions = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions"
)


def _queue_database(state_root: Path, columns: str = TASK_COLUMNS) -> Path:
    path = state_root / "run/queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(f"CREATE TABLE tasks({columns})")
    return path


def _execute(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(sql, parameters)


def _insert_task(path: Path, state: str, error_code: str | None, capability: str | None) -> None:
    _execute(
        path,
        "INSERT INTO tasks(id, state, error_code, blocked_capability) VALUES ('t', ?, ?, ?)",
        (state, error_code, capability),
    )


def _check(state_root: Path, deadline: float = float("inf")) -> dict[str, Any]:
    return doctor_queue.queue_check(state_root, NOW, deadline)


def test_a_queue_database_that_is_a_directory_is_refused_as_unsafe(tmp_path: Path) -> None:
    (tmp_path / "run/queue.sqlite3").mkdir(parents=True)

    check = _check(tmp_path)

    assert (check["status"], check["message"]) == ("error", "Queue database is unsafe.")
    assert check["details"]["read_error"] is True
    assert check["details"]["deletion_codes"] == ["queue_state_unreadable"]
    assert set(check["details"]["states"].values()) == {0}


def test_a_journal_without_its_queue_database_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run/queue.sqlite3-journal").write_bytes(b"journal")

    check = _check(tmp_path)

    assert (check["status"], check["message"]) == ("error", "Queue sidecars lack a database.")


def test_a_retired_queue_path_that_is_a_file_leaves_the_artifact_state_unknown(
    tmp_path: Path,
) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run/queue").write_bytes(b"not a directory")

    check = _check(tmp_path)

    assert check["status"] == "error"
    assert check["details"]["artifact_error"] is True
    assert check["details"]["deletion_codes"] == ["queue_artifact_state_unknown"]


def test_a_result_root_that_is_a_file_leaves_the_artifact_state_unknown(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run/queue-results").write_bytes(b"not a directory")

    check = _check(tmp_path)

    assert check["details"]["artifact_error"] is True
    assert check["status"] == "error"


@posix_permissions
@pytest.mark.parametrize("relative", ["run/queue", "run/queue-results"])
def test_an_unlistable_artifact_directory_leaves_the_state_unknown(
    tmp_path: Path, relative: str
) -> None:
    directory = tmp_path / relative
    directory.mkdir(parents=True)
    directory.chmod(0)
    try:
        check = _check(tmp_path)
    finally:
        directory.chmod(0o700)

    assert check["details"]["artifact_error"] is True
    assert "queue_artifact_state_unknown" in check["details"]["deletion_codes"]


def _reached_inside(function_name: str) -> Callable[[float], bool]:
    def reached(deadline: float) -> bool:
        return sys._getframe(1).f_code.co_name == function_name

    return reached


def test_a_deadline_reached_while_counting_results_reports_a_truncated_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "run/queue-results"
    results.mkdir(parents=True)
    (results / "a.json").write_bytes(b"{}")
    monkeypatch.setattr(monotonic_deadline, "deadline_reached", _reached_inside("_tally_artifacts"))

    check = _check(tmp_path)

    assert check["details"]["artifact_truncated"] is True
    assert check["details"]["results_retained"] == 0
    assert check["details"]["deletion_codes"] == ["queue_artifact_state_unknown"]


@pytest.mark.parametrize("function_name", ["_queue_scan_stop", "_scan_queue_database"])
def test_a_deadline_reached_inside_the_database_scan_is_unreadable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, function_name: str
) -> None:
    _insert_task(_queue_database(tmp_path), "succeeded", None, None)
    monkeypatch.setattr(monotonic_deadline, "deadline_reached", _reached_inside(function_name))

    check = _check(tmp_path)

    assert (check["status"], check["message"]) == ("error", "Queue state is unreadable.")
    assert check["details"]["deletion_codes"] == ["queue_state_unreadable"]


def test_a_scan_that_finishes_after_its_deadline_says_the_budget_was_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue_database(tmp_path)
    deadline = time.monotonic() + 3600
    monkeypatch.setattr(doctor_queue, "time", SimpleNamespace(monotonic=lambda: deadline))

    check = _check(tmp_path, deadline=deadline)

    assert check["details"]["budget_exhausted"] is True
    assert check["status"] == "ok"


def test_a_database_without_a_tasks_table_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "run/queue.sqlite3"
    path.parent.mkdir()
    _execute(path, "CREATE TABLE other(id TEXT)")

    check = _check(tmp_path)

    assert check["details"]["read_error"] is True
    assert check["message"] == "Queue state is unreadable."


@pytest.mark.parametrize(
    ("state", "error_code", "capability", "status"),
    [
        ("blocked", "needs_llm", "llm", "degraded"),
        ("blocked", "needs_llm", None, "error"),
        ("ready", "line\nbreak", None, "error"),
        ("ready", "x" * 201, None, "error"),
        ("dead", "attempts_exhausted", "llm", "error"),
        ("cancelled", "cancelled", None, "ok"),
    ],
)
def test_task_error_metadata_must_match_the_shape_its_state_allows(
    tmp_path: Path, state: str, error_code: str, capability: str | None, status: str
) -> None:
    _insert_task(_queue_database(tmp_path), state, error_code, capability)

    check = _check(tmp_path)

    assert check["status"] == status
    assert ("queue_state_corrupt" in check["details"]["deletion_codes"]) is (status == "error")


def test_a_blocked_task_names_its_code_and_capability(tmp_path: Path) -> None:
    _insert_task(_queue_database(tmp_path), "blocked", "needs_llm", "llm")

    details = _check(tmp_path)["details"]

    assert (details["codes"], details["capabilities"]) == (["needs_llm"], ["llm"])


def _ownership(path: Path, role: str, token: str | None, expires_at: str | None) -> None:
    _execute(
        path,
        "CREATE TABLE IF NOT EXISTS queue_ownership(role TEXT, token TEXT, "
        "pid INTEGER, expires_at TEXT)",
    )
    _execute(path, "INSERT INTO queue_ownership VALUES (?, ?, NULL, ?)", (role, token, expires_at))


def test_a_live_migration_owner_keeps_run_from_deletion(tmp_path: Path) -> None:
    path = _queue_database(tmp_path)
    _ownership(path, "migration", "token-1", (NOW + timedelta(minutes=5)).isoformat())

    details = _check(tmp_path)["details"]

    assert (details["live_migrations"], details["live_workers"]) == (1, 0)
    assert "queue_migration_live" in details["deletion_codes"]


def test_an_expired_owner_is_not_counted_live(tmp_path: Path) -> None:
    path = _queue_database(tmp_path)
    _ownership(path, "worker", "token-1", (NOW - timedelta(minutes=5)).isoformat())

    details = _check(tmp_path)["details"]

    assert (details["live_workers"], details["deletion_codes"]) == (0, [])


def test_an_owner_without_a_pid_or_expiry_leaves_ownership_unknown(tmp_path: Path) -> None:
    path = _queue_database(tmp_path)
    _ownership(path, "worker", "token-1", None)

    details = _check(tmp_path)["details"]

    assert details["deletion_codes"] == ["queue_owner_state_unknown"]
    assert details["live_workers"] == 0


def _result_file(state_root: Path, relative: str, content: bytes) -> None:
    path = state_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


def _task_with_result(path: Path, reference: str, digest: str) -> None:
    _execute(
        path,
        "INSERT INTO tasks(id, state, result_reference, result_sha256) "
        "VALUES ('t', 'succeeded', ?, ?)",
        (reference, digest),
    )


@posix_permissions
def test_a_result_whose_bytes_match_its_digest_is_valid(tmp_path: Path) -> None:
    path = _queue_database(tmp_path)
    _result_file(tmp_path, "run/queue-results/r.json", b'{"ok": true}')
    _task_with_result(path, "run/queue-results/r.json", hashlib.sha256(b'{"ok": true}').hexdigest())

    details = _check(tmp_path)["details"]

    assert details["results_invalid"] == 0


@posix_permissions
def test_a_result_whose_bytes_changed_is_invalid(tmp_path: Path) -> None:
    path = _queue_database(tmp_path)
    _result_file(tmp_path, "run/queue-results/r.json", b'{"ok": false}')
    _task_with_result(path, "run/queue-results/r.json", hashlib.sha256(b'{"ok": true}').hexdigest())

    check = _check(tmp_path)

    assert (check["status"], check["details"]["results_invalid"]) == ("error", 1)
    assert "queue_result_state_unknown" in check["details"]["deletion_codes"]


def test_a_result_outside_the_result_directory_is_invalid(tmp_path: Path) -> None:
    path = _queue_database(tmp_path)
    (tmp_path / "run/queue-results").mkdir()
    _result_file(tmp_path, "run/elsewhere/r.json", b"{}")
    _task_with_result(path, "run/elsewhere/r.json", hashlib.sha256(b"{}").hexdigest())

    assert _check(tmp_path)["details"]["results_invalid"] == 1


def _claim_database(claims: int, diagnostics: list[tuple[str, str]]) -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE claim(id INTEGER)")
    database.execute("CREATE TABLE claim_index_diagnostic(code TEXT, page TEXT)")
    database.executemany("INSERT INTO claim VALUES (?)", ((number,) for number in range(claims)))
    database.executemany("INSERT INTO claim_index_diagnostic VALUES (?, ?)", diagnostics)
    return database


def test_claims_past_the_operational_bound_are_reported_truncated() -> None:
    details: dict[str, Any] = {}
    with closing(_claim_database(MAX_OPERATIONAL_ROWS + 1, [])) as database:
        doctor_queue.count_claims(database, details)

    assert details["claims"] == MAX_OPERATIONAL_ROWS + 1
    assert details["codes"] == ["claim_scan_truncated"]


def test_claim_diagnostics_are_counted_by_code_and_page() -> None:
    details: dict[str, Any] = {}
    rows = [("stale", "a.md"), ("stale", "b.md"), ("orphan", "a.md")]
    with closing(_claim_database(2, rows)) as database:
        doctor_queue.count_claims(database, details)

    assert details["by_code"] == {"orphan": 1, "stale": 2}
    assert (details["pages"], details["pages_omitted"]) == (["a.md", "b.md"], 0)
