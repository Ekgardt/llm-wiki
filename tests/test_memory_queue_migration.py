"""Migration, retention, and purge tests for the SQLite memory queue."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue, MigrationBusy, QueueFailure  # noqa: E402
from reliable_memory import (  # noqa: E402
    OperationalDatabaseContractError,
    canonical_json_bytes,
    sha256_bytes,
)


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

    assert summary["payload_hash_mismatches"] == 1
    assert task == (expected, "0" * 64, "dead", "payload_hash_mismatch")
    assert history == [
        (
            1,
            "2026-08-05T12:00:00+00:00",
            "2026-08-05T12:00:01+00:00",
            "failed",
            "provider_failed",
        )
    ]
    assert claimable is None
    assert candidate_queue.claim("worker") is None


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
    monkeypatch.setattr(memory_queue, "_pid_is_alive", lambda pid: pid == os.getpid())
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


def test_live_processing_owner_aborts_without_marker(
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
    monkeypatch.setattr(
        memory_queue, "_pid_is_alive", lambda pid: pid in (42, os.getpid())
    )

    with pytest.raises(MigrationBusy) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "legacy_owner_live"
    assert not (tmp_path / "run" / "queue-migrated-v2").exists()
    assert (tmp_path / "run" / "queue.sqlite3").is_file()


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

    assert second.epoch == first.epoch + 1
    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._heartbeat_queue_owner(first, now=now + timedelta(seconds=2))
    assert raised.value.code == "migration_fence_lost"
    assert memory_queue._release_queue_owner(first) is False
    assert memory_queue._release_queue_owner(second) is True
    assert not list((tmp_path / "run").glob("queue-*.lock"))


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


def test_migration_fence_loss_aborts_before_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_legacy(tmp_path, "legacy.json", _legacy_task())
    real_scan = memory_queue._scan_legacy_records

    def steal_after_scan(path: Path):
        records = real_scan(path)
        with sqlite3.connect(tmp_path / "run" / "queue.sqlite3") as connection:
            connection.execute(
                """UPDATE queue_ownership
                   SET token='replacement', epoch=epoch+1,
                       expires_at='2099-01-01T00:00:00+00:00'
                   WHERE role='migration'"""
            )
        return records

    monkeypatch.setattr(memory_queue, "_scan_legacy_records", steal_after_scan)

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "migration_fence_lost"
    assert not (tmp_path / "run" / "queue-migrated-v2").exists()


def test_two_subprocess_contenders_over_stale_owner_run_exactly_one_migration(
    tmp_path: Path,
) -> None:
    _write_legacy(tmp_path, "legacy.json", _legacy_task())
    stale = memory_queue._acquire_queue_owner(
        tmp_path,
        "migration",
        "migration_busy",
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ttl_seconds=1,
    )
    with sqlite3.connect(tmp_path / "run" / "queue.sqlite3") as connection:
        connection.execute(
            "UPDATE queue_ownership SET pid=999999999 WHERE role='migration'"
        )
    start = tmp_path / "start"
    release = tmp_path / "release"
    entered = tmp_path / "entered"
    script = r"""
import json, sys, time
from pathlib import Path
import memory_queue as mq
root, start, release, entered = map(Path, sys.argv[1:])
real_scan = mq._scan_legacy_records
def paused(path):
    entered.write_text(str(mq.os.getpid()), encoding="ascii")
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return real_scan(path)
mq._scan_legacy_records = paused
while not start.exists():
    time.sleep(0.01)
try:
    receipt = mq.migrate_legacy_queue(root)
    print(json.dumps({"state": "migrated", "imported": receipt.imported}))
except mq.MigrationBusy as exc:
    print(json.dumps({"state": "busy", "code": exc.code}))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                str(start),
                str(release),
                str(entered),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    start.write_text("go", encoding="ascii")
    deadline = time.monotonic() + 10
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.exists()
    while not any(process.poll() is not None for process in processes) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sum(process.poll() is not None for process in processes) == 1
    release.write_text("go", encoding="ascii")
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))

    assert sum(item["state"] == "migrated" for item in outputs) == 1
    assert sum(item["state"] == "busy" for item in outputs) == 1
    assert not list((tmp_path / "run").glob("queue-*.lock"))
    assert stale.epoch >= 1


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


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b'{"version":1}',
        b'{"version":2,"extra":true}',
        b'{ "version": 2 }',
    ],
)
def test_invalid_migration_marker_is_stable_conflict_not_success(
    tmp_path: Path, raw: bytes
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    memory_queue._harden_owner_only(run_dir, 0o700)
    marker = run_dir / "queue-migrated-v2"
    marker.write_bytes(raw)
    memory_queue._harden_owner_only(marker, 0o600)
    _write_legacy(tmp_path, "legacy.json", _legacy_task())

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "migration_marker_invalid"
    assert (tmp_path / "run" / "queue" / "legacy.json").exists()


def test_marker_must_be_regular_owner_only_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_queue.migrate_legacy_queue(tmp_path)
    marker = tmp_path / "run" / "queue-migrated-v2"
    monkeypatch.setattr(
        memory_queue,
        "_is_owner_only",
        lambda path: False if Path(path) == marker else True,
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    assert raised.value.code == "migration_marker_invalid"


def test_legacy_enqueue_retracts_file_if_marker_wins_publication_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = memory_queue._write_durable_file
    published: list[Path] = []

    def marker_wins(path: Path, data: bytes) -> None:
        if path.parent.name == "queue" and path.suffix == ".json":
            marker = tmp_path / "run" / "queue-migrated-v2"
            real_write(marker, canonical_json_bytes({"version": 2}))
            published.append(path)
        real_write(path, data)

    monkeypatch.setattr(memory_queue, "_write_durable_file", marker_wins)

    with pytest.raises(memory_queue.LegacyBackendDisabled) as raised:
        memory_queue._legacy_enqueue_file("query", {"prompt": "late"}, tmp_path)

    assert raised.value.code == "legacy_marker_race"
    assert len(published) == 1
    assert not published[0].exists()


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

    assert queue.get(task_id).state == "succeeded"
    assert not export.exists()

    monkeypatch.setattr(memory_queue, "begin_immediate", real_begin_immediate)
    receipt = queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export
    )
    assert receipt.task_ids == (task_id,)
    assert export.is_dir()


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

    assert not export.exists()
    assert not list(export.parent.glob(".purge.staging-*"))
    monkeypatch.setattr(memory_queue, "_write_durable_file", real_write)
    receipt = queue.purge(
        terminal_before=now - timedelta(days=30), export_path=export
    )
    assert receipt.task_ids == (task_id,)
    assert export.is_dir()
    assert not list(export.parent.glob(".purge.staging-*"))


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

    assert receipt.purged == 0
    assert queue.get(succeeded).state == "succeeded"
    assert queue.get(dead).state == "dead"
    assert queue.retains_run_directory() is True
