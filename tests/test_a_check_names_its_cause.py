"""A read that could not happen now is not a verdict on the records.

On one machine the installer's adoption check met a busy database and reported
`conflict` / `reliability_v3_record_invalid`; the installer then said session
capture was disabled, and doctor agreed. A direct check a minute later said
`adopted`. The inspection caught every exception and answered with one fixed
code. Here a real exclusive lock stands in for the busy writer.
See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import doctor
import pytest
import repair_installed_memory
from installed_memory_repair import inspect_installed_vault, repair_installed_vault

from tests.test_reliability_v3_adoption import _vault


@contextlib.contextmanager
def _held(database: Path) -> Iterator[None]:
    """A writer that holds the database, as a live transaction does."""
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        connection.execute("ROLLBACK")
        connection.close()


@pytest.fixture
def adopted(tmp_path: Path) -> tuple[Path, Path]:
    root, state_root = _vault(tmp_path)
    report = repair_installed_vault(
        root=root, state_root=state_root, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )
    assert report["details"]["adoption_state"] == "adopted"
    return root, state_root


def _summary(root: Path, state_root: Path) -> str:
    printed = io.StringIO()
    arguments = ["--root", str(root), "--state-root", str(state_root), "--check", "--summary"]
    with contextlib.redirect_stdout(printed):
        repair_installed_memory.main(arguments)
    return printed.getvalue().strip()


def test_a_busy_database_is_unreadable_and_named_not_invalid(
    adopted: tuple[Path, Path],
) -> None:
    root, state_root = adopted
    with _held(state_root / "run" / "queue-v3.sqlite3"):
        report = inspect_installed_vault(root=root, state_root=state_root)
        capture = doctor._capture_check(root, state_root, time.monotonic() + 60)

    assert (report["details"]["adoption_state"], report["blockers"]) == (
        "unreadable",
        [{"code": "reliability_v3_state_unreadable"}],
    )
    assert report["details"]["error"] == "OperationalError: database is locked"
    assert "Session capture is disabled" not in capture["message"]
    assert "database is locked" in capture["message"]


def test_the_installer_line_for_a_busy_database_claims_nothing_about_capture(
    adopted: tuple[Path, Path],
) -> None:
    root, state_root = adopted
    with _held(state_root / "run" / "queue-v3.sqlite3"):
        summary = _summary(root, state_root)

    assert summary == (
        "The Reliability V3 records could not be read just now; this says nothing about "
        "whether capture is enabled. Cause: reliability_v3_state_unreadable; "
        "OperationalError: database is locked"
    )


def test_the_same_vault_reads_adopted_once_the_writer_is_gone(
    adopted: tuple[Path, Path],
) -> None:
    root, state_root = adopted
    assert _summary(root, state_root) == "Reliability V3 is adopted; session capture is enabled."


def test_the_repair_command_names_what_stopped_it(tmp_path: Path) -> None:
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = repair_installed_memory.main(["--root", "vault\x00name", "--summary"])
    assert (code, printed.getvalue().strip()) == (
        2,
        "Reliability V3 state is 'unknown'. Cause: repair_backend_error; ValueError: embedded null byte",
    )


@pytest.mark.parametrize("argument", ["--root", "--state-root"])
def test_nul_path_is_rejected_even_when_resolution_is_permissive(tmp_path, monkeypatch, argument):
    monkeypatch.setattr(Path, "resolve", lambda self, **kwargs: self)
    monkeypatch.setattr(repair_installed_memory, "inspect_installed_vault", lambda **kwargs: {"overall_status": "ok"})
    arguments = ["--root", str(tmp_path), "--summary", argument, "vault\x00name"]
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = repair_installed_memory.main(arguments)
    assert code == 2
    assert "ValueError: embedded null byte" in printed.getvalue()


def test_expired_certification_sql_is_a_timeout_not_invalid_records(adopted, monkeypatch):
    from types import SimpleNamespace

    import installed_memory_repair as repair
    import reliable_memory

    expired = [False]
    monkeypatch.setattr(reliable_memory, "time", SimpleNamespace(monotonic=lambda: 101.0 if expired[0] else 99.0))

    def certify(database_name, database):
        expired[0] = True
        database.execute(
            "WITH RECURSIVE work(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM work WHERE n<?) SELECT sum(n) FROM work",
            (reliable_memory._OPERATIONAL_PROGRESS_VM_STEPS * 2,),
        ).fetchone()

    monkeypatch.setattr(repair, "_certify_active_database_contents", certify)
    root, state_root = adopted
    with pytest.raises(TimeoutError):
        repair.require_reliability_v3_adopted(root=root, state_root=state_root, deadline=100.0)


@pytest.mark.parametrize("deadline,now,code,message", [
    (None, 101.0, 9, "interrupted"),
    (100.0, 99.0, 9, "interrupted"),
    (100.0, 101.0, 11, "interrupted"),
    (100.0, 101.0, 11, "database disk image is malformed"),
])
def test_sqlite_deadline_normalization_preserves_other_causes(monkeypatch, deadline, now, code, message):
    from types import SimpleNamespace

    import installed_memory_repair as repair
    import reliable_memory

    monkeypatch.setattr(reliable_memory, "time", SimpleNamespace(monotonic=lambda: now))
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    with pytest.raises(sqlite3.OperationalError) as observed:
        repair._raise_adoption_sqlite_error(error, deadline)
    assert observed.value is error


def test_sqlite_interrupt_without_structured_code_supports_python310(monkeypatch):
    from types import SimpleNamespace

    import installed_memory_repair as repair
    import reliable_memory

    monkeypatch.setattr(reliable_memory, "time", SimpleNamespace(monotonic=lambda: 101.0))
    with pytest.raises(TimeoutError):
        repair._raise_adoption_sqlite_error(sqlite3.OperationalError("interrupted"), 100.0)
