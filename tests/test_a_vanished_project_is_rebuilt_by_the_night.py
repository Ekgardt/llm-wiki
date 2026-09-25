"""The nightly rebuilds a project whose journal fell behind its committed checkpoints.

Project `main` on the live vault had checkpoints 1–36 committed and no directory;
sequence 37 failed `ProjectJournalRebuildRequired` every night, the step returned 0
and the pass said `failures=0`. See
docs/research/2026-09-24-a-vanished-project-is-rebuilt-by-the-night.md.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS.parent / "scripts"
for entry in (str(SCRIPTS_DIR), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import doctor  # noqa: E402
import project_journal  # noqa: E402
import repair_orphaned_checkpoint_names as night  # noqa: E402
from project_journal import ProjectJournalRebuildRequired, ProjectStore  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402


def _event(index: int) -> dict[str, object]:
    return checkpoint_event(f"evt-{index}", f"rb:event-{index}")


def _vanished_project(tmp_path: Path) -> tuple[Path, ProjectStore]:
    vault = tmp_path / "vault"
    (vault / "knowledge/projects/demo").mkdir(parents=True)
    store = ProjectStore(vault, tmp_path / "state")
    for index in (1, 2, 3):
        store.checkpoint("demo", _event(index), "agent-a")
    shutil.rmtree(vault / "knowledge/projects/demo")
    with pytest.raises(ProjectJournalRebuildRequired):
        store.checkpoint("demo", _event(4), "agent-a")
    return vault, store


def test_the_night_rebuilds_the_journal_and_settles_the_parked_sequence(tmp_path: Path) -> None:
    vault, store = _vanished_project(tmp_path)

    lines = night.repair(store)

    journal = (vault / "knowledge/projects/demo/journal.md").read_bytes()
    sequences = [event["sequence"] for event in project_journal.parse_journal_events("demo", journal)]
    assert ("rebuilt:" in lines[0], night.any_failed(lines), sequences) == (True, False, [1, 2, 3, 4])


def test_a_journal_ahead_of_the_store_is_never_rebuilt_over(tmp_path: Path) -> None:
    _vault, store = _vanished_project(tmp_path)

    outcome = night._rebuilt(store, ProjectJournalRebuildRequired("demo", 4, 9))

    assert outcome.startswith(night.FAILED)


def test_a_row_the_night_cannot_settle_fails_the_step() -> None:
    lines = ["demo 4 (quarantined, evt-4…): failed: RuntimeError: nope", "x 1 (reserved, a…): written"]

    assert (night.any_failed(lines), night.any_failed(lines[1:])) == (True, False)


def _checkpoint_database(path: Path, attempts: list[str]) -> None:
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE project_checkpoints (project, sequence, state, operation_id)"
        )
        database.execute("CREATE TABLE project_checkpoint_attempts (project, sequence, created_at)")
        database.execute('CREATE TABLE "transaction" (operation_id, created_at)')
        database.execute("INSERT INTO project_checkpoints VALUES ('demo', 4, 'reserved', 'op')")
        for created in attempts:
            database.execute(
                "INSERT INTO project_checkpoint_attempts VALUES ('demo', 4, ?)", (created,)
            )


def test_doctor_ages_a_stuck_sequence_from_its_first_attempt(tmp_path: Path) -> None:
    path = tmp_path / "markdown-transactions-v3.sqlite3"
    _checkpoint_database(path, ["2026-09-23T21:18:00Z", "2026-09-24T03:09:20Z"])
    now = datetime(2026, 9, 24, 3, 9, 37, tzinfo=timezone.utc)

    rows = doctor._checkpoint_head_rows(path)

    assert (rows[0]["created_at"], doctor._checkpoint_stuck(rows[0], now)) == ("2026-09-23T21:18:00Z", True)


def test_an_unreadable_checkpoint_database_is_not_ok(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "markdown-transactions-v3.sqlite3").write_bytes(b"not a database" * 100)

    check = doctor._checkpoint_check(tmp_path, datetime.now(timezone.utc))

    assert check["status"] == "degraded"
