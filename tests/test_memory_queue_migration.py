"""Migration, retention, and purge tests for the SQLite memory queue."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue, MigrationBusy, QueueFailure  # noqa: E402
from reliable_memory import canonical_json_bytes, sha256_bytes  # noqa: E402


def _legacy_task(task_id: str = "legacy-1", **changes: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": task_id,
        "type": "query",
        "enqueued_at": "2026-07-01T12:00:00+00:00",
        "attempts": 3,
        "last_attempt_at": "2026-07-02T13:00:00+00:00",
        "payload": {"prompt": "hello", "password": "secret"},
    }
    task.update(changes)
    return task


def _write_legacy(root: Path, name: str, task: object) -> Path:
    queue_dir = root / "run" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / name
    path.write_text(json.dumps(task), encoding="utf-8")
    return path


def test_migration_imports_json_and_dead_processing_before_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = _write_legacy(tmp_path, "legacy-1.json", _legacy_task())
    processing = _write_legacy(
        tmp_path,
        "legacy-2.processing",
        _legacy_task(
            "legacy-2",
            attempts=2,
            lease_pid=987654321,
            lease_token="lease",
            lease_acquired_at="2026-07-03T14:00:00+00:00",
        ),
    )
    monkeypatch.setattr(memory_queue, "_pid_is_alive", lambda pid: False)
    observed_marker: list[bool] = []
    real_import = memory_queue._import_legacy_record

    def observed_import(*args, **kwargs):
        observed_marker.append((tmp_path / "run" / "queue-migrated-v2").exists())
        return real_import(*args, **kwargs)

    monkeypatch.setattr(memory_queue, "_import_legacy_record", observed_import)

    receipt = memory_queue.migrate_legacy_queue(tmp_path)

    assert receipt.imported == 2
    assert receipt.quarantined == 0
    assert observed_marker == [False, False]
    assert (tmp_path / "run" / "queue-migrated-v2").is_file()
    assert not ready.exists() and not processing.exists()
    queue = MemoryQueue(tmp_path)
    first = queue.get("legacy-1")
    second = queue.get("legacy-2")
    assert (first.attempts, first.created_at, first.last_attempt_at) == (
        3,
        datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
        datetime(2026, 7, 2, 13, tzinfo=timezone.utc),
    )
    assert second.attempts == 2
    assert second.last_attempt_at == datetime(2026, 7, 3, 14, tzinfo=timezone.utc)
    assert first.payload["password"] == "[REDACTED]"


def test_live_processing_owner_aborts_without_marker_or_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_legacy(
        tmp_path,
        "live.processing",
        _legacy_task(
            lease_pid=42,
            lease_token="live",
            lease_acquired_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    monkeypatch.setattr(memory_queue, "_pid_is_alive", lambda pid: pid == 42)

    with pytest.raises(MigrationBusy) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "legacy_owner_live"
    assert not (tmp_path / "run" / "queue-migrated-v2").exists()
    assert not (tmp_path / "run" / "queue.sqlite3").exists()


def test_concurrent_migration_has_one_exclusive_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_legacy(tmp_path, "legacy.json", _legacy_task())
    entered = threading.Event()
    release = threading.Event()
    real_scan = memory_queue._scan_legacy_records

    def paused_scan(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(memory_queue, "_scan_legacy_records", paused_scan)
    outcomes: list[object] = []

    def migrate() -> None:
        try:
            outcomes.append(memory_queue.migrate_legacy_queue(tmp_path))
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcomes.append(exc)

    owner = threading.Thread(target=migrate)
    owner.start()
    assert entered.wait(5)
    contender = threading.Thread(target=migrate)
    contender.start()
    contender.join(5)
    release.set()
    owner.join(5)

    assert sum(isinstance(item, memory_queue.MigrationReceipt) for item in outcomes) == 1
    busy = [item for item in outcomes if isinstance(item, MigrationBusy)]
    assert len(busy) == 1 and busy[0].code == "migration_busy"


def test_late_upgraded_legacy_write_cannot_recreate_queue_during_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_legacy(tmp_path, "legacy.json", _legacy_task())
    renamed = threading.Event()
    release = threading.Event()
    real_scan = memory_queue._scan_legacy_records

    def paused_scan(path: Path):
        assert path.name.startswith("queue-migration-")
        assert not (tmp_path / "run" / "queue").exists()
        renamed.set()
        assert release.wait(5)
        return real_scan(path)

    monkeypatch.setattr(memory_queue, "_scan_legacy_records", paused_scan)
    outcome: list[object] = []

    def migrate() -> None:
        try:
            outcome.append(memory_queue.migrate_legacy_queue(tmp_path))
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=migrate)
    thread.start()
    assert renamed.wait(5)
    with pytest.raises(memory_queue.LegacyBackendDisabled) as raised:
        memory_queue._legacy_enqueue_file("query", {"prompt": "late"}, tmp_path)
    assert raised.value.code == "legacy_migration_quiesced"
    assert not (tmp_path / "run" / "queue").exists()
    release.set()
    thread.join(5)
    assert isinstance(outcome[0], memory_queue.MigrationReceipt)


def test_recreated_legacy_queue_after_marker_is_quarantined_as_conflict(
    tmp_path: Path,
) -> None:
    memory_queue.migrate_legacy_queue(tmp_path)
    raw = b'{"token":"late-secret"}'
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir()
    (queue_dir / "late.json").write_bytes(raw)
    with pytest.raises(memory_queue.LegacyBackendDisabled) as raised:
        memory_queue._legacy_enqueue_file("query", {"prompt": "late"}, tmp_path)

    assert raised.value.code == "legacy_backend_conflict"
    quarantine = tmp_path / "run" / "queue-quarantine"
    assert next(quarantine.glob("*.raw")).read_bytes() == raw
    assert not queue_dir.exists()


def test_malformed_legacy_record_is_redacted_quarantined_and_not_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = b'{"id":"broken","payload":{"token":"top-secret"}}'
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir(parents=True)
    source = queue_dir / "broken.json"
    source.write_bytes(raw)

    receipt = memory_queue.migrate_legacy_queue(tmp_path)

    assert receipt.imported == 0 and receipt.quarantined == 1
    assert receipt.codes == ("legacy_invalid",)
    assert not source.exists()
    quarantine_dir = tmp_path / "run" / "queue-quarantine"
    quarantine = next(quarantine_dir.glob("*.json"))
    raw_copy = next(quarantine_dir.glob("*.raw"))
    text = quarantine.read_text(encoding="utf-8")
    assert "top-secret" not in text
    assert set(json.loads(text)) == {"code", "raw_name", "source_name", "source_sha256"}
    assert raw_copy.read_bytes() == raw
    assert memory_queue._is_owner_only(quarantine_dir)
    assert memory_queue._is_owner_only(quarantine)
    assert memory_queue._is_owner_only(raw_copy)
    assert "top-secret" not in capsys.readouterr().out
    assert MemoryQueue(tmp_path).retains_run_directory() is True


def test_legacy_source_is_bounded_and_never_follows_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"token":"outside-secret"}')
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir(parents=True)
    link = queue_dir / "broken.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(memory_queue, "_MAX_LEGACY_RECORD_BYTES", 8)

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "legacy_source_unsafe"
    assert outside.read_bytes() == b'{"token":"outside-secret"}'
    assert not (tmp_path / "run" / "queue-migrated-v2").exists()


def test_identical_malformed_sources_get_distinct_quarantine_metadata(
    tmp_path: Path,
) -> None:
    raw = b'{"token":"same-secret"}'
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "first.json").write_bytes(raw)
    (queue_dir / "second.json").write_bytes(raw)

    receipt = memory_queue.migrate_legacy_queue(tmp_path)

    quarantine = tmp_path / "run" / "queue-quarantine"
    assert receipt.quarantined == 2
    assert len(list(quarantine.glob("*.raw"))) == 1
    assert len(list(quarantine.glob("*.json"))) == 2


def test_unsafe_legacy_id_is_quarantined(tmp_path: Path) -> None:
    _write_legacy(tmp_path, "unsafe.json", _legacy_task("../outside"))

    receipt = memory_queue.migrate_legacy_queue(tmp_path)

    assert receipt.imported == 0
    assert receipt.quarantined == 1
    assert not (tmp_path / "outside.result").exists()


def test_conflicting_interrupted_import_aborts_without_marker(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, state,
                   priority, created_at, updated_at, available_at
               ) VALUES (
                   'legacy-1', 'query', 1, '{}',
                   '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
                   'ready', 0, '2026-07-01T12:00:00+00:00',
                   '2026-07-01T12:00:00+00:00', '2026-07-01T12:00:00+00:00'
               )"""
        )
    source = _write_legacy(tmp_path, "legacy-1.json", _legacy_task())

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "legacy_import_conflict"
    assert not source.exists()
    assert len(list((tmp_path / "run").glob("queue-migration-*/legacy-1.json"))) == 1
    assert not (tmp_path / "run" / "queue-migrated-v2").exists()


def test_marker_prevents_any_legacy_backend_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_queue.migrate_legacy_queue(tmp_path)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    task_id = memory_queue.enqueue("query", {"prompt": "sqlite"})

    assert MemoryQueue(tmp_path).get(task_id).state == "ready"
    assert not list((tmp_path / "run" / "queue").glob("*"))
    with pytest.raises(memory_queue.LegacyBackendDisabled) as raised:
        memory_queue._legacy_write_allowed(tmp_path)
    assert raised.value.code == "legacy_backend_disabled"


def test_redrive_links_new_task_without_changing_dead_history(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path)
    original = queue.enqueue("query", 1, {"prompt": "again"})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))
    before = queue.get(original)

    replacement = queue.redrive(original)

    assert replacement != original
    assert queue.get(replacement).redrive_of == original
    assert queue.get(replacement).state == "ready"
    assert queue.get(original) == before


def test_redrive_insert_and_link_commit_in_one_transaction(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path)
    original = queue.enqueue("query", 1, {"prompt": "again"})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))
    statements: list[str] = []
    real_connect = queue._connect

    def traced_connect():
        connection = real_connect()
        connection.set_trace_callback(statements.append)
        return connection

    queue._connect = traced_connect  # type: ignore[method-assign]

    replacement = queue.redrive(original)

    assert queue.get(replacement).redrive_of == original
    assert sum(item == "BEGIN IMMEDIATE" for item in statements) == 1
    inserts = [item for item in statements if item.startswith("INSERT INTO tasks")]
    assert len(inserts) == 1 and original in inserts[0]
    assert not any(item.startswith("UPDATE tasks SET redrive_of") for item in statements)


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

    assert receipt.purged == 1 and receipt.task_ids == (task_id,)
    manifest_bytes = (export / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["records_sha256"] == sha256_bytes((export / "records.json").read_bytes())
    assert manifest_bytes == canonical_json_bytes(manifest)
    assert (export / "results" / f"{task_id}.result").read_bytes() == b"answer"
    with pytest.raises(KeyError):
        queue.get(task_id)


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

    receipt = queue.purge(
        terminal_before=now + timedelta(days=1),
        export_path=tmp_path / "export",
    )

    assert receipt.purged == 0
    assert queue.get(succeeded).state == "succeeded"
    assert queue.get(dead).state == "dead"
    assert queue.retains_run_directory() is True
