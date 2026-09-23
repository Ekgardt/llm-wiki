"""Migration, retention, and purge tests for the SQLite memory queue."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import DEFAULTS, MemoryQueue, MigrationBusy, QueueFailure  # noqa: E402
from reliable_memory import (  # noqa: E402
    OperationalDatabaseContractError,
    canonical_json_bytes,
    sha256_bytes,
)


def test_queue_v2_backup_migrates_canonical_text_payload_to_identical_blob(
    tmp_path: Path,
) -> None:
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue(
        "query",
        3,
        {"city": "München", "prompt": "こんにちは", "items": ["é", "中"]},
    )
    expected = canonical_json_bytes(queue.get(task_id).payload)
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    summary = memory_queue.initialize_queue_v3_candidate(
        candidate, source_v2=queue.db_path
    )
    with sqlite3.connect(candidate) as database:
        stored = database.execute(
            "SELECT typeof(payload_blob), payload_blob, input_hash FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    assert summary["tasks"] == 1
    assert stored == ("blob", expected, sha256_bytes(expected))


def test_queue_v2_source_rows_reconcile_to_exact_v3_links_and_failures(
    tmp_path: Path,
) -> None:
    queue = MemoryQueue(tmp_path)
    logical_path = "knowledge/daily/2026-08-05.md"
    source_digest = "a" * 64
    task_id = queue.enqueue(
        "compile",
        1,
        {"source_path": logical_path, "source_digest": source_digest},
    )
    queue.record_source_failure(
        logical_path,
        source_digest,
        error_code="provider_failed",
        producer="queue",
    )
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    summary = memory_queue.initialize_queue_v3_candidate(
        candidate, source_v2=queue.db_path
    )

    with sqlite3.connect(candidate) as database:
        links = database.execute(
            """SELECT task_id, logical_path, source_digest
               FROM task_source_links ORDER BY task_id, logical_path, source_digest"""
        ).fetchall()
        failures = database.execute(
            """SELECT logical_path, source_digest, error_code, producer
               FROM source_failures ORDER BY logical_path, source_digest"""
        ).fetchall()
    assert summary["task_source_links"] == 1
    assert summary["source_failures"] == 1
    assert links == [(task_id, logical_path, source_digest)]
    assert failures == [
        (logical_path, source_digest, "provider_failed", "queue")
    ]


def test_fresh_queue_candidate_rejects_existing_migrated_rows(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path)
    queue.enqueue("query", 1, {"prompt": "already migrated"})
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=queue.db_path)

    with pytest.raises(OperationalDatabaseContractError) as raised:
        memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)

    assert raised.value.code == "queue_v3_source_conflict"


def test_queue_v2_hash_mismatch_is_preserved_dead_and_never_claimable(
    tmp_path: Path,
) -> None:
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("query", 1, {"prompt": "retain these exact bytes"})
    expected = canonical_json_bytes(queue.get(task_id).payload)
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            """INSERT INTO attempt_history(
                   task_id, attempt, started_at, finished_at, outcome, error_code
               ) VALUES (?, 1, ?, ?, 'failed', 'provider_failed')""",
            (
                task_id,
                "2026-08-05T12:00:00+00:00",
                "2026-08-05T12:00:01+00:00",
            ),
        )
        database.execute(
            "UPDATE tasks SET input_hash=?, state='ready', error_code=NULL WHERE id=?",
            ("0" * 64, task_id),
        )
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    summary = memory_queue.initialize_queue_v3_candidate(
        candidate, source_v2=queue.db_path
    )
    candidate_queue = MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)

    with sqlite3.connect(candidate) as database:
        task = database.execute(
            "SELECT payload_blob, input_hash, state, error_code FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        history = database.execute(
            """SELECT attempt, started_at, finished_at, outcome, error_code
               FROM attempt_history WHERE task_id=? ORDER BY sequence""",
            (task_id,),
        ).fetchall()
        claimable = database.execute(
            """SELECT id FROM tasks
               WHERE state='ready' AND available_at <= '9999-12-31T23:59:59+00:00'
               ORDER BY priority DESC, available_at, created_at, id LIMIT 1"""
        ).fetchone()

    assert (
        summary["payload_hash_mismatches"],
        task,
        history,
        (claimable, candidate_queue.claim("worker")),
    ) == (
        1,
        (expected, "0" * 64, "dead", "payload_hash_mismatch"),
        [
            (
                1,
                "2026-08-05T12:00:00+00:00",
                "2026-08-05T12:00:01+00:00",
                "failed",
                "provider_failed",
            )
        ],
        (None, None),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"b":1,"a":2}',
        json.dumps(
            {"value": "x" * (256 * 1024 + 1)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ],
    ids=("noncanonical", "oversized-string"),
)
def test_queue_v2_hash_valid_invalid_payload_is_preserved_dead(
    tmp_path: Path, payload: bytes
) -> None:
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("query", 1, {"prompt": "replace me"})
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET payload_json=?, input_hash=? WHERE id=?",
            (payload.decode("utf-8"), sha256_bytes(payload), task_id),
        )
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"

    summary = memory_queue.initialize_queue_v3_candidate(
        candidate, source_v2=queue.db_path
    )

    with sqlite3.connect(candidate) as database:
        row = database.execute(
            "SELECT payload_blob, input_hash, state, error_code FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    assert summary["payload_hash_mismatches"] == 1
    assert row == (
        payload,
        sha256_bytes(payload),
        "dead",
        "payload_hash_mismatch",
    )


def test_sqlite_owner_takeover_is_epoch_fenced_and_release_is_token_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    first = memory_queue._acquire_queue_owner(
        tmp_path, "migration", "migration_busy", now=now, ttl_seconds=10
    )
    monkeypatch.setattr(memory_queue, "_pid_is_alive", lambda pid: False)
    second = memory_queue._acquire_queue_owner(
        tmp_path,
        "migration",
        "migration_busy",
        now=now + timedelta(seconds=1),
        ttl_seconds=10,
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._heartbeat_queue_owner(first, now=now + timedelta(seconds=2))

    assert (
        second.epoch - first.epoch,
        raised.value.code,
        (
            memory_queue._release_queue_owner(first),
            memory_queue._release_queue_owner(second),
        ),
        list((tmp_path / "run").glob("queue-*.lock")),
    ) == (1, "migration_fence_lost", (False, True), [])


def test_expired_owner_cannot_be_stolen_while_pid_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    owner = memory_queue._acquire_queue_owner(
        tmp_path, "migration", "migration_busy", now=now, ttl_seconds=1
    )
    monkeypatch.setattr(memory_queue, "_pid_is_alive", lambda pid: pid == owner.pid)

    with pytest.raises(MigrationBusy) as raised:
        memory_queue._acquire_queue_owner(
            tmp_path,
            "migration",
            "migration_busy",
            now=now + timedelta(seconds=2),
            ttl_seconds=1,
        )

    assert raised.value.code == "migration_busy"
    assert memory_queue._release_queue_owner(owner) is True


def _await_file(path: Path, timeout: float) -> None:
    """Wait for a child to signal through the filesystem."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path.name} never appeared")


def test_redrive_links_new_task_without_changing_dead_history(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path)
    original = queue.enqueue("query", 1, {"prompt": "again"})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))
    before = queue.get(original)

    replacement = queue.redrive(original)
    fresh = queue.get(replacement)

    assert (
        replacement == original,
        (fresh.redrive_of, fresh.state),
        queue.get(original) == before,
    ) == (False, (original, "ready"), True)


def test_redrive_insert_and_link_commit_in_one_transaction(tmp_path: Path) -> None:
    from contextlib import contextmanager

    queue = MemoryQueue(tmp_path)
    original = queue.enqueue("query", 1, {"prompt": "again"})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))
    statements: list[str] = []
    real_connect = queue._connect

    @contextmanager
    def traced_connect():
        with real_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    queue._connect = traced_connect  # type: ignore[method-assign]

    replacement = queue.redrive(original)

    inserts = _statements_starting("INSERT INTO tasks", statements)

    # One transaction, one insert that already names its parent, no later update.
    assert (
        queue.get(replacement).redrive_of,
        _statements_starting("BEGIN IMMEDIATE", statements),
        (len(inserts), original in inserts[0]),
        _statements_starting("UPDATE tasks SET redrive_of", statements),
    ) == (original, ["BEGIN IMMEDIATE"], (1, True), [])


def _statements_starting(prefix: str, statements: list[str]) -> list[str]:
    return [item for item in statements if item.startswith(prefix)]


def test_purge_requires_cutoff_and_export_then_verifies_before_deleting(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    task_id = queue.enqueue("query", 1, {"prompt": "done"})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"answer")
    queue.acknowledge(lease)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?",
            ((now - timedelta(days=31)).isoformat(timespec="microseconds"), task_id),
        )

    export = tmp_path / "exports" / "purge-1"
    receipt = queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export
    )

    manifest_bytes = (export / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    with pytest.raises(KeyError):
        queue.get(task_id)

    # The export carries the result and a canonical manifest over its records.
    assert (
        (receipt.purged, receipt.task_ids),
        manifest["records_sha256"],
        manifest_bytes == canonical_json_bytes(manifest),
        (export / "results" / f"{task_id}.result").read_bytes(),
    ) == (
        (1, (task_id,)),
        sha256_bytes((export / "records.json").read_bytes()),
        True,
        b"answer",
    )


def test_purge_expired_deadline_creates_no_export_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"answer")
    queue.acknowledge(lease)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?",
            ((now - timedelta(days=31)).isoformat(timespec="microseconds"), task_id),
        )
    export = tmp_path / "exports" / "purge"

    with pytest.raises(TimeoutError, match="deadline"):
        queue.purge(
            terminal_before=now - timedelta(days=30),
            export_path=export,
            deadline=time.monotonic() - 1,
        )

    assert queue.get(task_id).state == "succeeded"
    assert not export.exists()


def test_purge_rolls_back_when_deadline_expires_after_delete_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"answer")
    queue.acknowledge(lease)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?",
            ((now - timedelta(days=31)).isoformat(timespec="microseconds"), task_id),
        )
    expired = False

    def cancelled() -> bool:
        return expired

    @contextmanager
    def expire_before_commit(connection, *, before_commit=None):
        nonlocal expired
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            expired = True
            if before_commit is not None:
                before_commit()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    real_begin_immediate = memory_queue.begin_immediate
    monkeypatch.setattr(memory_queue, "begin_immediate", expire_before_commit)

    export = tmp_path / "exports" / "purge"
    with pytest.raises(TimeoutError, match="deadline"):
        queue.purge(
            terminal_before=now - timedelta(days=30),
            export_path=export,
            deadline=time.monotonic() + 5,
            cancelled=cancelled,
        )

    rolled_back = (queue.get(task_id).state, export.exists())

    monkeypatch.setattr(memory_queue, "begin_immediate", real_begin_immediate)
    receipt = queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export
    )

    # Nothing was lost by the expiry, and the next attempt completes.
    assert (rolled_back, receipt.task_ids, export.is_dir()) == (
        ("succeeded", False),
        (task_id,),
        True,
    )


def test_purge_build_failure_cleans_staging_and_retry_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"answer")
    queue.acknowledge(lease)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?",
            ((now - timedelta(days=31)).isoformat(timespec="microseconds"), task_id),
        )
    export = tmp_path / "exports" / "purge"
    real_write = memory_queue._write_durable_file
    failed = False

    def fail_manifest(path: Path, data: bytes) -> None:
        nonlocal failed
        if path.name == "manifest.json" and not failed:
            failed = True
            raise OSError("secret path must not escape")
        real_write(path, data)

    monkeypatch.setattr(memory_queue, "_write_durable_file", fail_manifest)
    with pytest.raises(OSError):
        queue.purge(terminal_before=now - timedelta(days=30), export_path=export)

    after_failure = (export.exists(), list(export.parent.glob(".purge.staging-*")))

    monkeypatch.setattr(memory_queue, "_write_durable_file", real_write)
    receipt = queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export
    )

    # The failed attempt left no staging directory, and the retry completes.
    assert (
        after_failure,
        (receipt.task_ids, export.is_dir()),
        list(export.parent.glob(".purge.staging-*")),
    ) == ((False, []), ((task_id,), True), [])


def test_purge_rejects_unsafe_existing_export_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    parent = tmp_path / "exports"
    parent.mkdir()
    monkeypatch.setattr(
        memory_queue,
        "_is_owner_only",
        lambda path: False if Path(path) == parent else True,
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        queue.purge(
            terminal_before=datetime.now(timezone.utc),
            export_path=parent / "purge",
        )

    assert raised.value.code == "export_parent_permissions_invalid"


def test_default_retention_excludes_recent_terminal_and_all_dead(tmp_path: Path) -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    succeeded = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=succeeded, result=b"")
    queue.acknowledge(lease)
    dead = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))

    export_parent = tmp_path / "exports"
    export_parent.mkdir()
    memory_queue._harden_owner_only(export_parent, 0o700)
    receipt = queue.purge(
        terminal_before=now + timedelta(days=1),
        export_path=export_parent / "export",
    )

    assert (
        receipt.purged,
        (queue.get(succeeded).state, queue.get(dead).state),
        queue.retains_run_directory(),
    ) == (0, ("succeeded", "dead"), True)


def _dead_task(queue: MemoryQueue, now: datetime, prompt: str) -> str:
    """Exhaust one task's attempts so it reaches the dead state."""
    task_id = queue.enqueue("query", 1, {"prompt": prompt})
    for _ in range(DEFAULTS.queue_max_attempts):
        lease = queue.claim("worker")
        if lease is None:
            break
        queue.fail(lease, QueueFailure("processor_failed"))
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET state='dead', error_code='attempts_exhausted', updated_at=? "
            "WHERE id=?",
            ((now - timedelta(days=31)).isoformat(timespec="microseconds"), task_id),
        )
    return task_id


def test_a_dead_task_is_retained_by_default_and_purged_only_when_asked(
    tmp_path: Path,
) -> None:
    """Attempts-exhausted work is evidence; retiring it is an explicit action."""
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    task_id = _dead_task(queue, now, "never finished")
    cutoff = now - timedelta(days=30)

    default_receipt = queue.purge(
        terminal_before=cutoff, export_path=tmp_path / "exports" / "default"
    )

    assert default_receipt.purged == 0
    assert queue.get(task_id).state == "dead"

    export = tmp_path / "exports" / "with-dead"
    receipt = queue.purge(
        terminal_before=cutoff, export_path=export, include_dead=True
    )

    assert receipt.task_ids == (task_id,)
    records = json.loads((export / "records.json").read_bytes())
    assert [item["state"] for item in records] == ["dead"]
    with pytest.raises(KeyError):
        queue.get(task_id)


def test_a_purged_task_can_be_restored_from_its_verified_export(tmp_path: Path) -> None:
    """Deletion without a way back would lose the work, not just its record."""
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    task_id = _dead_task(queue, now, "restore me")
    export = tmp_path / "exports" / "restorable"
    queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export, include_dead=True
    )

    receipt = queue.restore(export_path=export)

    exported_id, restored_id = receipt.task_ids[0]
    restored = queue.get(restored_id)

    assert (
        (receipt.restored, exported_id),
        (restored.state, restored.kind, restored.payload),
    ) == ((1, task_id), ("ready", "query", {"prompt": "restore me"}))


def test_a_tampered_export_restores_nothing(tmp_path: Path) -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    queue = MemoryQueue(tmp_path, clock=lambda: now)
    _dead_task(queue, now, "tampered")
    export = tmp_path / "exports" / "tampered"
    queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export, include_dead=True
    )
    records = json.loads((export / "records.json").read_bytes())
    records[0]["payload"] = {"prompt": "rewritten"}
    (export / "records.json").write_bytes(canonical_json_bytes(records))

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        queue.restore(export_path=export)

    assert raised.value.code == "restore_verification_failed"
    assert queue.list_tasks() == []
