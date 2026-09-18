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

from tests.slow_machine import LONG_TIMEOUT, PAUSE_TIMEOUT

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

    queue = MemoryQueue(tmp_path)
    first = queue.get("legacy-1")
    second = queue.get("legacy-2")

    # The marker is written only after every record is in, and the sources go.
    assert (
        (receipt.imported, receipt.quarantined),
        observed_marker,
        (
            (tmp_path / "run" / "queue-migrated-v2").is_file(),
            ready.exists(),
            processing.exists(),
        ),
        (first.attempts, first.created_at, first.last_attempt_at),
        (second.attempts, second.last_attempt_at, first.payload["password"]),
    ) == (
        (2, 0),
        [False, False],
        (True, False, False),
        (
            3,
            datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
            datetime(2026, 7, 2, 13, tzinfo=timezone.utc),
        ),
        (2, datetime(2026, 7, 3, 14, tzinfo=timezone.utc), "[REDACTED]"),
    )


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
        assert release.wait(PAUSE_TIMEOUT)
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
    assert entered.wait(LONG_TIMEOUT)
    contender = threading.Thread(target=migrate)
    contender.start()
    contender.join(LONG_TIMEOUT)
    release.set()
    owner.join(LONG_TIMEOUT)

    busy = [item for item in outcomes if isinstance(item, MigrationBusy)]
    assert (
        sum(isinstance(item, memory_queue.MigrationReceipt) for item in outcomes),
        [item.code for item in busy],
    ) == (1, ["migration_busy"])


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


def _await_file(path: Path, timeout: float) -> None:
    """Wait for a child to signal through the filesystem."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path.name} never appeared")


def _await_first_exit(processes: list[subprocess.Popen], timeout: float) -> None:
    """Wait for the first contender to finish, whichever one it is."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            return
        time.sleep(0.01)
    raise AssertionError("no contender finished before the deadline")


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
    deadline = time.monotonic() + 180
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
    # Each wait gets its own budget sized for the slowest supported machine:
    # spawning two interpreters that import the queue module is itself slow on
    # a hosted Windows image, and the second wait must not inherit what the
    # first one spent.
    _await_file(entered, LONG_TIMEOUT)
    _await_first_exit(processes, LONG_TIMEOUT)
    finished_before_release = _finished(processes)
    release.write_text("go", encoding="ascii")
    outputs = [_contender_output(process) for process in processes]

    # One was refused before the other was let go, so only one migration ran.
    assert (
        finished_before_release,
        sorted(item["state"] for item in outputs),
        list((tmp_path / "run").glob("queue-*.lock")),
        stale.epoch >= 1,
    ) == (1, ["busy", "migrated"], [], True)


def _finished(processes: list[subprocess.Popen]) -> int:
    return sum(process.poll() is not None for process in processes)


def _contender_output(process: subprocess.Popen) -> dict:
    stdout, stderr = process.communicate(timeout=PAUSE_TIMEOUT)
    assert process.returncode == 0, stderr
    return json.loads(stdout)


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
        assert release.wait(PAUSE_TIMEOUT)
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
    assert renamed.wait(LONG_TIMEOUT)
    # Nothing can recreate the legacy directory while the migration holds it:
    # the pre-adoption JSON writer was removed with the rest of that path.
    absent_during_migration = not (tmp_path / "run" / "queue").exists()
    release.set()
    thread.join(LONG_TIMEOUT)

    assert (absent_during_migration, isinstance(outcome[0], memory_queue.MigrationReceipt)) == (
        True,
        True,
    )


def test_recreated_legacy_queue_after_marker_is_quarantined_as_conflict(
    tmp_path: Path,
) -> None:
    memory_queue.migrate_legacy_queue(tmp_path)
    raw = b'{"token":"late-secret"}'
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir()
    (queue_dir / "late.json").write_bytes(raw)

    # A directory that reappears after the marker is a conflict, and the next
    # migration pass is what notices it.
    with pytest.raises(memory_queue.LegacyBackendDisabled) as raised:
        memory_queue.migrate_legacy_queue(tmp_path)

    quarantine = tmp_path / "run" / "queue-quarantine"
    assert (
        raised.value.code,
        next(quarantine.glob("*.raw")).read_bytes(),
        queue_dir.exists(),
    ) == ("legacy_backend_conflict", raw, False)


def test_malformed_legacy_record_is_redacted_quarantined_and_not_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = b'{"id":"broken","payload":{"token":"top-secret"}}'
    queue_dir = tmp_path / "run" / "queue"
    queue_dir.mkdir(parents=True)
    source = queue_dir / "broken.json"
    source.write_bytes(raw)

    receipt = memory_queue.migrate_legacy_queue(tmp_path)

    quarantine_dir = tmp_path / "run" / "queue-quarantine"
    quarantine = next(quarantine_dir.glob("*.json"))
    raw_copy = next(quarantine_dir.glob("*.raw"))
    text = quarantine.read_text(encoding="utf-8")
    owner_only = tuple(
        memory_queue._is_owner_only(path)
        for path in (quarantine_dir, quarantine, raw_copy)
    )

    # The record is kept byte for byte, and its secret reaches neither the
    # quarantine note nor the console.
    assert (
        (receipt.imported, receipt.quarantined, receipt.codes),
        source.exists(),
        (set(json.loads(text)), "top-secret" in text),
        (raw_copy.read_bytes(), owner_only),
        ("top-secret" in capsys.readouterr().out, MemoryQueue(tmp_path).retains_run_directory()),
    ) == (
        (0, 1, ("legacy_invalid",)),
        False,
        ({"code", "raw_name", "source_name", "source_sha256"}, False),
        (raw, (True, True, True)),
        (False, True),
    )


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


def test_after_the_marker_every_enqueue_goes_to_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past migration there is no JSON path left to write to, only the database."""
    memory_queue.migrate_legacy_queue(tmp_path)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))

    task_id = memory_queue.enqueue("query", {"prompt": "sqlite"})

    assert (
        MemoryQueue(tmp_path).get(task_id).state,
        list((tmp_path / "run" / "queue").glob("*")),
    ) == ("ready", [])


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
