"""Doctor asks the writers' admission question and waits out a busy database as they do.

On the live vault a transaction held `markdown-transactions-v3.sqlite3` for a
moment; the writers' admission waited and went on, but doctor's single attempt
reported `adoption: error, every Markdown writer is refused`, and the installer's
smoke aborted the update on it. A real exclusive lock stands in for the writer.
The same install first failed with a bare "Doctor did not return valid JSON";
that refusal now carries doctor's exit code and the end of its stderr.
See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import doctor
import install_smoke
import pytest

from tests.test_a_check_names_its_cause import adopted  # noqa: F401  (fixture)

_HELD_SECONDS = 0.5


def _hold_briefly(database: Path, taken: threading.Event) -> None:
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("BEGIN EXCLUSIVE")
    taken.set()
    time.sleep(_HELD_SECONDS)
    connection.execute("ROLLBACK")
    connection.close()


def test_a_writer_holding_the_database_for_a_moment_is_not_a_refusal(
    adopted: tuple[Path, Path],  # noqa: F811
) -> None:
    root, state_root = adopted
    taken = threading.Event()
    writer = threading.Thread(
        target=_hold_briefly, args=(state_root / "run" / "markdown-transactions-v3.sqlite3", taken)
    )
    writer.start()
    taken.wait()
    check = doctor._adoption_check(root, state_root)
    writer.join()

    assert (check["status"], check["message"]) == ("ok", "The adoption record admits writers.")


def test_a_doctor_reply_that_is_not_a_report_is_named_by_its_exit_and_stderr() -> None:
    crashed = subprocess.run(
        [sys.executable, "-c", "print('partial'); raise SystemExit('doctor broke here')"],
        capture_output=True,
        text=True,
    )
    with pytest.raises(install_smoke.SmokeFailure) as refused:
        install_smoke._checked_doctor_report(crashed)

    assert str(refused.value) == "Doctor did not return valid JSON (exit 1; stderr: doctor broke here)"
