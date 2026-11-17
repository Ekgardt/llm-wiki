from __future__ import annotations

import os
import random
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import queue_core
from queue_core import MemoryQueue

START = datetime(2026, 7, 14, 12, tzinfo=UTC)


class SteppingClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    vault.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return vault, state_root


def test_an_expired_lease_is_recovered_back_to_ready(tmp_path: Path) -> None:
    clock = SteppingClock()
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(7))
    task_id = queue.enqueue("test", 1, {"page": "a"})
    lease = queue.claim("worker", lease_seconds=30)
    assert lease is not None and lease.id == task_id

    clock.now = START + timedelta(seconds=31)

    assert queue.recover_expired_leases() == 1
    assert queue.get(task_id).state == "ready"
    assert queue.recover_expired_leases() == 0


def test_the_write_connection_seam_reads_the_queue_tables(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path, rng=random.Random(7))
    task_id = queue.enqueue("test", 1, {"page": "a"})

    with queue.connection() as database:
        row = database.execute("SELECT id, state FROM tasks").fetchone()

    assert tuple(row) == (task_id, "ready")


def _drop_columns(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as database, database:
        database.execute("ALTER TABLE source_fences DROP COLUMN heartbeat_at")
        database.execute("ALTER TABLE source_fences DROP COLUMN expires_at")
        database.execute("ALTER TABLE tasks DROP COLUMN last_attempt_at")
        database.execute(
            "INSERT INTO source_fences(daily_id, source_digest, token, owner_pid, acquired_at) "
            "VALUES ('2026-01-01', ?, 'token', 1, '2026-07-01T00:00:00Z')",
            ("d" * 64,),
        )


def _columns(database_path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(database_path)) as database:
        return {str(row[1]) for row in database.execute(f"PRAGMA table_info({table})")}


def test_an_older_queue_schema_gains_the_missing_columns_with_backfill(tmp_path: Path) -> None:
    MemoryQueue(tmp_path)
    database_path = tmp_path / "run/queue.sqlite3"
    _drop_columns(database_path)

    MemoryQueue(tmp_path)

    assert {"heartbeat_at", "expires_at"} <= _columns(database_path, "source_fences")
    assert "last_attempt_at" in _columns(database_path, "tasks")
    with closing(sqlite3.connect(database_path)) as database:
        fence = database.execute("SELECT heartbeat_at, expires_at FROM source_fences").fetchone()
    assert fence == ("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z")


def test_the_repair_commands_open_the_candidate_queue_before_adoption(
    roots: tuple[Path, Path],
) -> None:
    _, state_root = roots
    candidate = state_root / "run/queue-v3.candidate.sqlite3"
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)

    queue = queue_core.v3_queue_for_cli()

    assert Path(queue.db_path) == candidate


def test_a_restore_requeues_the_work_a_purge_exported(roots: tuple[Path, Path]) -> None:
    _, state_root = roots
    exports = state_root / "exports"
    exports.mkdir(mode=0o700)
    task_id = queue_core.default_queue().enqueue("test", 1, {"page": "a"})
    assert queue_core.cancel(task_id) is True
    _age_task(state_root / "run/queue.sqlite3", task_id)
    purged = queue_core.purge(
        terminal_before=datetime.now(UTC) + timedelta(days=1), export_path=exports / "one"
    )
    assert purged.task_ids == (task_id,)

    restored = queue_core.restore(export_path=exports / "one")

    assert restored.restored == 1
    original_id, restored_id = restored.task_ids[0]
    assert original_id == task_id
    assert queue_core.default_queue().get(restored_id).state == "ready"


def _age_task(database_path: Path, task_id: str) -> None:
    with closing(sqlite3.connect(database_path)) as database, database:
        database.execute(
            "UPDATE tasks SET updated_at='2020-01-01T00:00:00.000000Z' WHERE id=?", (task_id,)
        )


def test_a_restore_without_an_export_manifest_is_refused(roots: tuple[Path, Path]) -> None:
    _, state_root = roots

    with pytest.raises(FileNotFoundError, match=r"manifest\.json"):
        queue_core.restore(export_path=state_root / "missing-export")


@pytest.mark.parametrize("limits", [(-1, 0, 0), (0, -1, 0), (0, 0, -1)])
def test_a_worker_with_a_negative_limit_is_refused(limits: tuple[int, int, int]) -> None:
    max_tasks, max_seconds, idle_seconds = limits

    with pytest.raises(ValueError, match="worker limits must be non-negative"):
        queue_core.run_worker(
            lambda payload: True,
            max_tasks=max_tasks,
            max_seconds=max_seconds,
            idle_seconds=idle_seconds,
        )


def test_the_redacted_parser_hides_the_message_of_a_failing_exit() -> None:
    parser = queue_core.RedactedArgumentParser(prog="memory_queue")

    with pytest.raises(queue_core.InvalidArguments):
        parser.exit(2, "secret detail")


def test_the_redacted_parser_still_exits_cleanly_on_success() -> None:
    parser = queue_core.RedactedArgumentParser(prog="memory_queue")

    with pytest.raises(SystemExit) as exited:
        parser.exit(0)

    assert exited.value.code == 0


def test_a_retired_json_queue_that_is_a_symlink_counts_as_unknown(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "run/queue").symlink_to(tmp_path / "elsewhere")

    assert queue_core._json_queue_entries(tmp_path) == 1


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_unlistable_retired_json_queue_counts_as_unknown(tmp_path: Path) -> None:
    directory = tmp_path / "run/queue"
    directory.mkdir(parents=True)
    directory.chmod(0)
    try:
        counted = queue_core._json_queue_entries(tmp_path)
    finally:
        directory.chmod(0o700)

    assert counted == 1


def test_a_retired_json_queue_counts_each_entry(tmp_path: Path) -> None:
    directory = tmp_path / "run/queue"
    directory.mkdir(parents=True)
    for name in ("a.json", "b.json"):
        (directory / name).write_bytes(b"{}")

    assert queue_core._json_queue_entries(tmp_path) == 2
