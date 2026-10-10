"""Queue health judges all retained facts within its deadline, beyond the old cap."""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import doctor
import pytest

from tests.slow_machine import LONG_TIMEOUT

OLD_SCAN_CAP = 10_000


def queue_database(state: Path) -> Path:
    path = state / "run/queue.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, state TEXT, error_code TEXT, blocked_capability TEXT, result_reference TEXT, result_sha256 TEXT, redrive_of TEXT, updated_at TEXT)")
        database.executemany("INSERT INTO tasks(id,state) VALUES (?,'succeeded')", ((f"task-{i}",) for i in range(OLD_SCAN_CAP+1)))
    return path


def health(state: Path):
    return doctor._queue_check(state, datetime.now(timezone.utc), time.monotonic()+LONG_TIMEOUT)


def test_every_healthy_task_is_counted_past_the_old_ceiling(tmp_path: Path):
    queue_database(tmp_path)
    result = health(tmp_path)
    assert result["status"] == "ok"
    assert result["details"]["states"]["succeeded"] == OLD_SCAN_CAP+1
    assert "queue_scan_truncated" not in result["details"]["codes"]
    assert "queue_state_unknown" not in result["details"]["deletion_codes"]
    assert "queue_task_retained" in result["details"]["deletion_codes"]


def test_a_real_metadata_defect_beyond_the_old_ceiling_is_found(tmp_path: Path):
    path = queue_database(tmp_path)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE tasks SET blocked_capability='invalid-for-succeeded' WHERE rowid=1")
    result = health(tmp_path)
    assert result["status"] == "error"
    assert "queue_state_corrupt" in result["details"]["deletion_codes"]


@pytest.mark.parametrize("table,retained", [("source_failures", "queue_source_failure_retained"), ("source_fences", "queue_source_fence_retained")])
def test_every_side_table_record_is_counted(tmp_path: Path, table: str, retained: str):
    path = queue_database(tmp_path)
    with sqlite3.connect(path) as database:
        database.execute(f'CREATE TABLE "{table}"(id TEXT)')
        database.executemany(f'INSERT INTO "{table}" VALUES (?)', ((str(i),) for i in range(OLD_SCAN_CAP+2)))
    result = health(tmp_path)
    assert result["details"][table] == OLD_SCAN_CAP+2
    assert retained in result["details"]["deletion_codes"]
    assert f"queue_{table[:-1]}_state_unknown" not in result["details"]["deletion_codes"]


def test_expired_empty_owner_records_do_not_create_unknown_state_by_count(tmp_path: Path):
    path = queue_database(tmp_path)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE queue_ownership(role TEXT,token TEXT,pid INTEGER,expires_at TEXT)")
        database.executemany("INSERT INTO queue_ownership VALUES ('worker',NULL,0,NULL)", (() for _ in range(OLD_SCAN_CAP+1)))
    result = health(tmp_path)
    assert "queue_owner_state_unknown" not in result["details"]["deletion_codes"]


def test_every_modern_result_artifact_is_counted(tmp_path: Path):
    queue_database(tmp_path)
    results = tmp_path / "run/queue-results"
    results.mkdir()
    for i in range(OLD_SCAN_CAP+1):
        (results / f"retained-{i}.json").write_bytes(b"{}")
    result = health(tmp_path)
    assert result["details"]["results_retained"] == OLD_SCAN_CAP+1
    assert result["details"]["artifact_truncated"] is False
    assert "queue_result_retained" in result["details"]["deletion_codes"]


def test_expired_deadline_preserves_unknown_state_without_alleging_corruption(tmp_path: Path):
    queue_database(tmp_path)
    result = doctor._queue_check(tmp_path, datetime.now(timezone.utc), time.monotonic())
    assert result["details"]["read_error"] is True
    assert "queue_state_unreadable" in result["details"]["deletion_codes"]
    assert "queue_state_corrupt" not in result["details"]["deletion_codes"]


def test_a_bad_result_digest_beyond_the_old_ceiling_is_found(tmp_path: Path):
    path = queue_database(tmp_path)
    results = tmp_path / "run/queue-results"
    results.mkdir()
    (results / "old-result.json").write_bytes(b"{}")
    with sqlite3.connect(path) as database:
        database.execute("UPDATE tasks SET result_reference='run/queue-results/old-result.json', result_sha256=? WHERE rowid=1", ("f"*64,))
    result = health(tmp_path)
    assert result["status"] == "error"
    assert result["details"]["results_invalid"] == 1
    assert "queue_result_state_unknown" in result["details"]["deletion_codes"]


def test_result_artifacts_outside_the_vault_are_still_refused(tmp_path: Path):
    path = queue_database(tmp_path)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE tasks SET result_reference='../outside.json', result_sha256=? WHERE rowid=1", ("f"*64,))
    result = health(tmp_path)
    assert result["details"]["results_invalid"] == 1
    assert "queue_result_state_unknown" in result["details"]["deletion_codes"]


def test_a_dead_task_answered_by_a_retained_redrive_is_not_unresolved(tmp_path: Path):
    path = queue_database(tmp_path)
    with sqlite3.connect(path) as database:
        database.execute("UPDATE tasks SET state='dead', error_code='attempts_exhausted', updated_at=? WHERE rowid=1", (datetime.now(timezone.utc).isoformat(),))
        database.execute("UPDATE tasks SET redrive_of='task-0' WHERE rowid=2")
    result = health(tmp_path)
    assert result["details"]["states"]["dead"] == 1
    assert result["details"]["dead_unresolved"] == 0
