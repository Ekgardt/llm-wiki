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
        "Reliability V3 state is 'unknown'. Cause: repair_backend_error; "
        "ValueError: embedded null byte",
    )
