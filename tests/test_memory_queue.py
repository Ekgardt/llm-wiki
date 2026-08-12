"""Tests for memory_queue.py — persistent task queue.

Locks in:
1. enqueue/list_pending/mark_attempt round-trip.
2. Drain processes tasks via callback, marks success/failure correctly.
3. Failed tasks increment attempt counter; permanently-failed (>=5) skipped.
4. status() returns the expected shape for the metacognitive block.
5. Corrupted or unreadable queue entries make integrity visibly unavailable.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def clean_queue(tmp_path, monkeypatch):
    """Point memory_queue at a tmp dir for isolation."""
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    # Force re-import so module-level env reads pick up monkeypatched value.
    if "memory_queue" in sys.modules:
        del sys.modules["memory_queue"]
    import memory_queue

    queue_path = tmp_path / "queue"
    queue_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(memory_queue, "_queue_path", lambda: queue_path)
    monkeypatch.setattr(memory_queue, "_queue_dir", lambda: queue_path)
    return memory_queue


def test_enqueue_creates_json_file(clean_queue):
    task_id = clean_queue.enqueue("compile", {"daily": "2026-07-03.md"})
    assert task_id.startswith("20260703-") or task_id  # date prefix or any id

    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["type"] == "compile"
    assert pending[0]["payload"]["daily"] == "2026-07-03.md"
    assert pending[0]["attempts"] == 0


def test_queue_dir_prefers_current_explicit_state_root(tmp_path, monkeypatch):
    import memory_queue
    import memory_state

    stale_root = tmp_path / "stale"
    current_root = tmp_path / "current"
    monkeypatch.setattr(memory_state, "STATE_ROOT", stale_root)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(current_root))

    assert memory_queue._queue_dir() == current_root / "run" / "queue"


def test_queue_rejects_nul_explicit_state_root_before_filesystem_access(
    tmp_path,
    monkeypatch,
):
    import memory_queue

    monkeypatch.setattr(
        memory_queue.os,
        "environ",
        {
            **os.environ,
            "LLM_WIKI_STATE_ROOT": f"{tmp_path / 'runtime'}\0suffix",
        },
    )

    with pytest.raises(ValueError, match="LLM_WIKI_STATE_ROOT contains NUL"):
        memory_queue._state_root()

    assert not (tmp_path / "runtime").exists()


def test_mark_attempt_success_deletes_task(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "hello"})
    assert len(clean_queue.list_pending()) == 1

    clean_queue.mark_attempt(task_id, success=True)
    assert len(clean_queue.list_pending()) == 0


def test_mark_attempt_failure_increments_counter(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "hello"})
    clean_queue.mark_attempt(task_id, success=False)
    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_attempt_at"] is not None


def test_mark_attempt_rejects_traversal_shaped_task_id(clean_queue, tmp_path):
    """A task ID must not escape run/queue through the filename glob."""
    sentinel = tmp_path / "queue-parent-sentinel.json"
    sentinel.write_text('{"must": "survive"}', encoding="utf-8")

    clean_queue.mark_attempt("../queue-parent-sentinel", success=True)

    assert sentinel.exists()


def test_drain_processes_all_success(clean_queue):
    for i in range(3):
        clean_queue.enqueue("query", {"prompt": f"q{i}"})

    seen: list[str] = []
    def processor(task):
        seen.append(task["payload"]["prompt"])
        return True

    counts = clean_queue.drain_with(processor)
    assert counts == {"ok": 3, "failed": 0, "skipped": 0, "pending": 0}
    assert sorted(seen) == ["q0", "q1", "q2"]
    assert len(clean_queue.list_pending()) == 0


def test_drain_marks_failed_and_continues(clean_queue):
    clean_queue.enqueue("query", {"prompt": "ok"})
    clean_queue.enqueue("query", {"prompt": "fail"})

    def processor(task):
        return task["payload"]["prompt"] != "fail"

    counts = clean_queue.drain_with(processor)
    assert counts["ok"] == 1
    assert counts["failed"] == 1
    pending = clean_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["payload"]["prompt"] == "fail"
    assert pending[0]["attempts"] == 1


def test_drain_skips_permanently_failed(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "stuck"})
    # Pre-fail 5 times to mark as permanently failed.
    for _ in range(5):
        clean_queue.mark_attempt(task_id, success=False)

    called = []
    def processor(task):
        called.append(task["id"])
        return True

    counts = clean_queue.drain_with(processor)
    assert counts["skipped"] == 1
    assert counts["ok"] == 0
    assert called == []  # processor never invoked


def test_drain_skips_recently_attempted(clean_queue):
    """Tasks attempted <60s ago are skipped (backoff)."""
    task_id = clean_queue.enqueue("query", {"prompt": "x"})
    clean_queue.mark_attempt(task_id, success=False)

    called = []
    def processor(task):
        called.append(task["id"])
        return True

    counts = clean_queue.drain_with(processor)
    # Task was just attempted (failed) → should be skipped on immediate retry.
    assert counts["skipped"] == 1
    assert called == []


def test_drain_respects_max_tasks_limit(clean_queue):
    for i in range(10):
        clean_queue.enqueue("query", {"prompt": f"q{i}"})

    counts = clean_queue.drain_with(lambda t: True, max_tasks=3)
    assert counts["ok"] == 3
    assert len(clean_queue.list_pending()) == 7


def test_status_returns_expected_shape(clean_queue):
    clean_queue.enqueue("compile", {"daily": "2026-07-03.md"})
    clean_queue.enqueue("query", {"prompt": "x"})
    task_id = clean_queue.enqueue("query", {"prompt": "stuck"})
    for _ in range(5):
        clean_queue.mark_attempt(task_id, success=False)

    s = clean_queue.status()
    assert s["pending_total"] == 3
    assert s["by_type"]["compile"] == 1
    assert s["by_type"]["query"] == 2
    assert s["permanently_failed"] == 1
    assert s["permanently_failed_ids"] == [task_id]
    assert "queue_dir" in s


@pytest.mark.parametrize("operation", ("list", "status", "prepare", "drain"))
def test_broken_json_makes_queue_integrity_visibly_unavailable_without_mutation(
    clean_queue,
    operation,
):
    task_id = clean_queue.enqueue("query", {"prompt": "must remain durable"})
    queue_dir = clean_queue._queue_dir()
    task_path = queue_dir / f"{task_id}.json"
    original = task_path.read_bytes()
    (queue_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
    processor_calls = []

    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        if operation == "list":
            clean_queue.list_pending()
        elif operation == "status":
            clean_queue.status()
        elif operation == "prepare":
            clean_queue.prepare_sdk_task()
        else:
            clean_queue.drain_with(
                lambda task: processor_calls.append(task["id"]) or True
            )

    assert task_path.read_bytes() == original
    assert processor_calls == []
    assert list(queue_dir.glob("*.processing")) == []


@pytest.mark.parametrize("suffix", (".JSON", ".Processing"))
@pytest.mark.parametrize("operation", ("list", "status", "prepare", "claim", "drain"))
def test_case_variant_task_suffix_fails_closed_without_mutation(
    clean_queue,
    suffix,
    operation,
):
    task_id = clean_queue.enqueue("query", {"prompt": "must remain durable"})
    queue_dir = clean_queue._queue_dir()
    task_path = queue_dir / f"{task_id}.json"
    original_task = task_path.read_bytes()
    case_variant = queue_dir / f"case-variant{suffix}"
    case_variant.write_bytes(b"must remain untouched")
    original_variant = case_variant.read_bytes()
    processor_calls = []

    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        if operation == "list":
            clean_queue.list_pending()
        elif operation == "status":
            clean_queue.status()
        elif operation == "prepare":
            clean_queue.prepare_sdk_task()
        elif operation == "claim":
            clean_queue._claim_next_task()
        else:
            clean_queue.drain_with(
                lambda task: processor_calls.append(task["id"]) or True
            )

    assert task_path.read_bytes() == original_task
    assert case_variant.read_bytes() == original_variant
    assert processor_calls == []
    assert [path for path in queue_dir.iterdir() if path.suffix == ".processing"] == []


def test_oversized_task_json_makes_integrity_unavailable(clean_queue, monkeypatch):
    queue_dir = clean_queue._queue_dir()
    oversized = queue_dir / "oversized.json"
    oversized.write_text(
        json.dumps({"payload": "x" * 256}),
        encoding="utf-8",
    )
    monkeypatch.setattr(clean_queue, "MAX_QUEUE_TASK_BYTES", 64, raising=False)

    with pytest.raises(RuntimeError, match="exceeds.*byte limit"):
        clean_queue.status()


def test_queue_total_entry_cap_counts_non_task_entries(clean_queue, monkeypatch):
    queue_dir = clean_queue._queue_dir()
    for index in range(3):
        (queue_dir / f"noise-{index}.txt").write_text("noise", encoding="utf-8")
    monkeypatch.setattr(clean_queue, "MAX_QUEUE_ENTRIES", 2, raising=False)

    with pytest.raises(RuntimeError, match="entry limit"):
        clean_queue.list_pending()


def test_unreadable_task_makes_integrity_unavailable(clean_queue, monkeypatch):
    task_id = clean_queue.enqueue("query", {"prompt": "unreadable"})
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    original = task_path.read_bytes()
    real_open = Path.open

    def denied_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == task_path and "r" in mode:
            raise PermissionError("queue task read denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)

    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        clean_queue.list_pending()

    with open(task_path, "rb") as handle:
        assert handle.read() == original


def test_symlinked_task_is_rejected_without_reading_or_mutating_target(
    clean_queue,
    tmp_path,
):
    queue_dir = clean_queue._queue_dir()
    outside = tmp_path / "outside-task.json"
    outside_payload = json.dumps(
        {
            "id": "20260730-120000-00000001",
            "type": "query",
            "enqueued_at": "2026-07-30T12:00:00",
            "enqueue_sequence": 1,
            "attempts": 0,
            "last_attempt_at": None,
            "payload": {"prompt": "outside"},
        }
    ).encode()
    outside.write_bytes(outside_payload)
    linked = queue_dir / "linked.json"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(RuntimeError, match="not a regular file"):
        clean_queue.list_pending()

    assert linked.is_symlink()
    assert outside.read_bytes() == outside_payload


def test_nonregular_task_entry_makes_integrity_unavailable(clean_queue):
    directory = clean_queue._queue_dir() / "directory.json"
    directory.mkdir()

    with pytest.raises(RuntimeError, match="not a regular file"):
        clean_queue.status()


def test_symlinked_queue_root_is_not_followed(clean_queue, tmp_path, monkeypatch):
    external = tmp_path / "external-queue"
    external.mkdir()
    external_task = external / "outside.json"
    external_task.write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked-queue"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    monkeypatch.setattr(clean_queue, "_queue_dir", lambda: linked)

    with pytest.raises(RuntimeError, match="queue directory.*not a regular directory"):
        clean_queue.list_pending()

    assert linked.is_symlink()
    assert external_task.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("invalid_case", ("array", "utf8", "surrogate", "deep"))
def test_strict_task_reader_rejects_invalid_object_graphs(
    clean_queue,
    monkeypatch,
    invalid_case,
):
    path = clean_queue._queue_dir() / f"{invalid_case}.json"
    if invalid_case == "array":
        path.write_bytes(b"[]")
    elif invalid_case == "utf8":
        path.write_bytes(b'{"invalid":"\xff"}')
    elif invalid_case == "surrogate":
        path.write_bytes(b'{"invalid":"\\ud800"}')
    else:
        monkeypatch.setattr(clean_queue, "MAX_QUEUE_JSON_DEPTH", 4, raising=False)
        path.write_text(
            '{"nested":' + ("[" * 8) + "null" + ("]" * 8) + "}",
            encoding="utf-8",
        )

    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        clean_queue.list_pending()


@pytest.mark.parametrize("operation", ("list", "status", "prepare", "drain"))
def test_schema_malformed_object_is_never_skipped_as_zero_work(
    clean_queue,
    operation,
):
    malformed = clean_queue._queue_dir() / "empty-object.json"
    malformed.write_text("{}", encoding="utf-8")
    original = malformed.read_bytes()

    with pytest.raises(RuntimeError, match="queue task schema is invalid"):
        if operation == "list":
            clean_queue.list_pending()
        elif operation == "status":
            clean_queue.status()
        elif operation == "prepare":
            clean_queue.prepare_sdk_task()
        else:
            clean_queue.drain_with(lambda _task: True)

    assert malformed.read_bytes() == original
    assert list(clean_queue._queue_dir().glob("*.processing")) == []


@pytest.mark.parametrize("operation", ("inventory", "prepare"))
def test_noncanonical_task_id_fails_closed_before_lease_on_repeated_reads(
    clean_queue,
    operation,
):
    queue_dir = clean_queue._queue_dir()
    path = queue_dir / "x.json"
    path.write_text(
        json.dumps(
            {
                "id": "x",
                "type": "query",
                "enqueued_at": "2026-07-30T12:00:00",
                "enqueue_sequence": 1,
                "attempts": 0,
                "last_attempt_at": None,
                "payload": {"prompt": "must remain pending"},
            }
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()

    for _ in range(2):
        with pytest.raises(
            clean_queue.QueueIntegrityError,
            match="queue task id is not canonical",
        ):
            if operation == "inventory":
                clean_queue._inventory_queue_locked(queue_dir)
            else:
                clean_queue.prepare_sdk_task()

        assert path.read_bytes() == original
        assert not path.with_suffix(".processing").exists()
        assert not path.with_suffix(".acquiring").exists()


@pytest.mark.parametrize("operation", ("prepare", "drain"))
def test_task_filename_identity_mismatch_is_never_skipped(
    clean_queue,
    operation,
):
    path = clean_queue._queue_dir() / "wrong-name.json"
    path.write_text(
        json.dumps(
            {
                "id": "20260730-120000-00000002",
                "type": "query",
                "enqueued_at": "2026-07-30T12:00:00",
                "enqueue_sequence": 1,
                "attempts": 0,
                "last_attempt_at": None,
                "payload": {"prompt": "must not disappear"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="filename does not match"):
        if operation == "prepare":
            clean_queue.prepare_sdk_task()
        else:
            clean_queue.drain_with(lambda _task: True)

    assert path.exists()
    assert list(clean_queue._queue_dir().glob("*.processing")) == []


def test_incomplete_inventory_blocks_sdk_acknowledgement(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "apply only when complete"})
    prepared = clean_queue.prepare_sdk_task()
    queue_dir = clean_queue._queue_dir()
    lease_path = queue_dir / f"{task_id}.processing"
    original = lease_path.read_bytes()
    (queue_dir / "broken.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        clean_queue.apply_sdk_result(
            {**prepared, "success": True, "response": "must not apply"}
        )

    assert lease_path.read_bytes() == original
    assert not (queue_dir.parent / "queue-results" / f"{task_id}.txt").exists()


def test_incomplete_inventory_blocks_enqueue_and_mark_attempt(clean_queue):
    task_id = clean_queue.enqueue("query", {"prompt": "preserve"})
    queue_dir = clean_queue._queue_dir()
    task_path = queue_dir / f"{task_id}.json"
    original = task_path.read_bytes()
    (queue_dir / "broken.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        clean_queue.enqueue("query", {"prompt": "must not enqueue"})
    with pytest.raises(RuntimeError, match="queue integrity unavailable"):
        clean_queue.mark_attempt(task_id, success=True)

    assert task_path.read_bytes() == original
    assert len(list(queue_dir.glob("*.json"))) == 2


def test_queue_sequence_read_is_strictly_bounded(clean_queue, monkeypatch):
    sequence = clean_queue._queue_dir().parent / "queue-sequence"
    sequence.write_text("123456789\n", encoding="utf-8")
    monkeypatch.setattr(clean_queue, "MAX_QUEUE_SEQUENCE_BYTES", 4, raising=False)

    with pytest.raises(RuntimeError, match="sequence.*byte limit"):
        clean_queue.enqueue("query", {"prompt": "must not enqueue"})

    assert list(clean_queue._queue_dir().glob("*.json")) == []


def test_queue_migration_journal_read_is_strictly_bounded(
    clean_queue,
    monkeypatch,
):
    migration = clean_queue._queue_dir().parent / "queue-migration.json"
    migration.write_text(
        json.dumps({"assignments": [], "padding": "x" * 128}),
        encoding="utf-8",
    )
    monkeypatch.setattr(clean_queue, "MAX_QUEUE_MIGRATION_BYTES", 32, raising=False)

    with pytest.raises(RuntimeError, match="migration.*byte limit"):
        clean_queue.list_pending()


def test_strict_queue_reader_preserves_valid_task_compatibility(clean_queue):
    first_id = clean_queue.enqueue(
        "query",
        {"prompt": "valid unicode caf\u00e9", "system_prompt": "rules"},
    )
    second_id = clean_queue.enqueue("compile", {"force": True})

    pending = clean_queue.list_pending()

    assert [task["id"] for task in pending] == [first_id, second_id]
    assert pending[0]["payload"]["prompt"] == "valid unicode caf\u00e9"


def test_list_pending_filters_by_age(clean_queue):
    """max_age_days filters out ancient tasks."""
    clean_queue.enqueue("query", {"prompt": "fresh"})
    # Manually write an old task.
    queue_dir = clean_queue._queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    old_task_id = "20200101-000000-00000001"
    old_task = {
        "id": old_task_id,
        "type": "query",
        "enqueued_at": "2020-01-01T00:00:00",
        "attempts": 0,
        "last_attempt_at": None,
        "payload": {"prompt": "ancient"},
    }
    (queue_dir / f"{old_task_id}.json").write_text(
        json.dumps(old_task), encoding="utf-8"
    )

    fresh_only = clean_queue.list_pending(max_age_days=30)
    assert len(fresh_only) == 1
    assert fresh_only[0]["payload"]["prompt"] == "fresh"

    all_tasks = clean_queue.list_pending()
    assert len(all_tasks) == 2


def test_fifo_sequence_survives_identical_timestamps_and_restart(
    clean_queue, monkeypatch
):
    real_datetime = clean_queue.datetime

    class FrozenDateTime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 7, 26, 12, 0, 0)

        @classmethod
        def fromisoformat(cls, value):
            return real_datetime.fromisoformat(value)

    monkeypatch.setattr(clean_queue, "datetime", FrozenDateTime)
    for index in range(10):
        clean_queue.enqueue("query", {"prompt": str(index)})

    first_read = clean_queue.list_pending()
    assert [task["payload"]["prompt"] for task in first_read] == [str(i) for i in range(10)]
    assert [task["enqueue_sequence"] for task in first_read] == list(range(1, 11))
    assert (clean_queue._queue_dir().parent / "queue-sequence").read_text() == "10\n"

    del sys.modules["memory_queue"]
    import memory_queue as restarted

    monkeypatch.setattr(restarted, "_queue_dir", clean_queue._queue_dir)
    second_read = restarted.list_pending()
    assert [task["payload"]["prompt"] for task in second_read] == [str(i) for i in range(10)]
    assert [task["enqueue_sequence"] for task in second_read] == list(range(1, 11))


def test_concurrent_producers_receive_unique_exact_fifo_sequences(clean_queue):
    producer_count = 50
    barrier = threading.Barrier(producer_count)
    errors = []

    def producer(index):
        try:
            barrier.wait()
            clean_queue.enqueue("query", {"prompt": str(index)})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=producer, args=(index,)) for index in range(producer_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not [thread for thread in threads if thread.is_alive()]
    pending = clean_queue.list_pending()
    assert len(pending) == producer_count
    assert [task["enqueue_sequence"] for task in pending] == list(range(1, producer_count + 1))
    assert {task["payload"]["prompt"] for task in pending} == {
        str(index) for index in range(producer_count)
    }


def test_concurrent_duplicate_flush_capture_enqueue_reuses_one_task(clean_queue):
    producer_count = 20
    barrier = threading.Barrier(producer_count)
    task_ids: list[str] = []
    errors: list[Exception] = []
    payload = {"prompt": "classify once", "capture_id": "a" * 64}

    def producer():
        try:
            barrier.wait()
            task_ids.append(clean_queue.enqueue("flush", payload))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=producer) for _ in range(producer_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not [thread for thread in threads if thread.is_alive()]
    assert len(set(task_ids)) == 1
    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_ids[0]
    assert pending["enqueue_sequence"] == 1


def test_duplicate_flush_capture_reuses_pending_task_id(clean_queue):
    payload = {"prompt": "classify pending", "capture_id": "b" * 64}

    first = clean_queue.enqueue("flush", payload)

    assert clean_queue.enqueue("flush", payload) == first
    assert [task["id"] for task in clean_queue.list_pending()] == [first]


def test_duplicate_flush_capture_reuses_processing_task_id(clean_queue):
    payload = {"prompt": "classify processing", "capture_id": "c" * 64}
    first = clean_queue.enqueue("flush", payload)
    prepared = clean_queue.prepare_sdk_task()

    assert prepared["task_id"] == first
    assert clean_queue.enqueue("flush", payload) == first
    assert clean_queue.list_pending() == []
    assert (clean_queue._queue_dir() / f"{first}.processing").exists()


@pytest.mark.parametrize(
    "capture_id",
    ("", "a" * 63, "A" * 64, "g" * 64, 7, None),
)
def test_flush_enqueue_rejects_noncanonical_capture_id(clean_queue, capture_id):
    with pytest.raises(ValueError, match="capture_id"):
        clean_queue.enqueue(
            "flush",
            {"prompt": "must reject malformed identity", "capture_id": capture_id},
        )

    assert clean_queue.list_pending() == []


def test_legacy_flush_enqueue_without_capture_id_keeps_distinct_tasks(clean_queue):
    first = clean_queue.enqueue("flush", {"prompt": "legacy"})
    second = clean_queue.enqueue("flush", {"prompt": "legacy"})

    assert first != second
    assert [task["id"] for task in clean_queue.list_pending()] == [first, second]


def test_cross_process_producers_serialize_durable_sequences(tmp_path):
    producer_count = 12
    env = {
        **os.environ,
        "LLM_WIKI_STATE_ROOT": str(tmp_path),
        "PYTHONPATH": str(SCRIPTS_DIR),
    }
    code = (
        "import sys; import memory_queue; "
        "memory_queue.enqueue('query', {'prompt': sys.argv[1]})"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(index)], env=env)
        for index in range(producer_count)
    ]

    assert [process.wait(timeout=15) for process in processes] == [0] * producer_count
    tasks = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "run" / "queue").glob("*.json")
    ]
    tasks.sort(key=lambda task: task["enqueue_sequence"])
    assert len(tasks) == producer_count
    assert [task["enqueue_sequence"] for task in tasks] == list(range(1, producer_count + 1))
    assert {task["payload"]["prompt"] for task in tasks} == {
        str(index) for index in range(producer_count)
    }


def test_legacy_tasks_are_deterministically_migrated_around_new_tasks(clean_queue):
    queue_dir = clean_queue._queue_dir()

    def write_legacy(name, enqueued_at, prompt, mtime):
        path = queue_dir / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "id": name,
                    "type": "query",
                    "enqueued_at": enqueued_at,
                    "attempts": 0,
                    "last_attempt_at": None,
                    "payload": {"prompt": prompt},
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))

    write_legacy("20200101-000000-00000001", "2020-01-01T00:00:00", "old", 1)
    clean_queue.enqueue("query", {"prompt": "new"})
    write_legacy("20300101-000000-00000002", "2030-01-01T00:00:00", "future", 2)

    pending = clean_queue.list_pending()
    assert [task["payload"]["prompt"] for task in pending] == ["old", "new", "future"]
    assert [task["enqueue_sequence"] for task in pending] == [1, 2, 3]
    assert all(
        "enqueue_sequence" in json.loads(path.read_text(encoding="utf-8"))
        for path in queue_dir.glob("*.json")
    )
    assert [task["payload"]["prompt"] for task in clean_queue.list_pending()] == [
        "old",
        "new",
        "future",
    ]


def test_canonical_legacy_task_leases_renews_and_failure_settles(clean_queue):
    queue_dir = clean_queue._queue_dir()
    task_id = "20200101-000000-00000001"
    path = queue_dir / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "type": "query",
                "enqueued_at": "2020-01-01T00:00:00",
                "attempts": 0,
                "last_attempt_at": None,
                "payload": {"prompt": "legacy without provenance"},
            }
        ),
        encoding="utf-8",
    )

    prepared = clean_queue.prepare_sdk_task()

    assert prepared["task_id"] == task_id
    assert clean_queue.renew_sdk_task(prepared) == (True, "renewed")
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": False, "error": "provider unavailable"}
    ) == (True, "failure recorded")
    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["enqueue_sequence"] == 1
    assert pending["attempts"] == 1
    assert pending["last_attempt_at"]
    assert pending["last_error"] == "provider unavailable"
    assert not path.with_suffix(".processing").exists()
    assert not path.with_suffix(".acquiring").exists()


def test_interrupted_legacy_migration_resumes_original_order(clean_queue, monkeypatch):
    queue_dir = clean_queue._queue_dir()
    for index in range(3):
        task_id = f"20200101-000000-{index + 1:08x}"
        path = queue_dir / f"{task_id}.json"
        path.write_text(
            json.dumps(
                {
                    "id": task_id,
                    "type": "query",
                    "enqueued_at": "2020-01-01T00:00:00",
                    "attempts": 0,
                    "last_attempt_at": None,
                    "payload": {"prompt": str(index)},
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, ns=(index + 1, index + 1))

    real_write = clean_queue._atomic_write_json
    writes = 0

    def interrupted_write(path, task):
        nonlocal writes
        real_write(path, task)
        writes += 1
        if writes == 1:
            raise OSError("simulated migration interruption")

    monkeypatch.setattr(clean_queue, "_atomic_write_json", interrupted_write)
    with pytest.raises(OSError, match="migration interruption"):
        clean_queue.list_pending()

    monkeypatch.setattr(clean_queue, "_atomic_write_json", real_write)
    pending = clean_queue.list_pending()
    assert [task["payload"]["prompt"] for task in pending] == ["0", "1", "2"]
    assert [task["enqueue_sequence"] for task in pending] == [1, 2, 3]


def test_fifo_sequence_lock_does_not_deadlock_sdk_lease(clean_queue):
    first_id = clean_queue.enqueue("query", {"prompt": "first"})
    clean_queue.enqueue("query", {"prompt": "second"})
    result = []
    thread = threading.Thread(target=lambda: result.append(clean_queue.prepare_sdk_task()))

    thread.start()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result[0]["task_id"] == first_id


def _transitional_flush_payload(**overrides):
    payload = {
        "prompt": "classify transitional work",
        "event": "pre-compact",
        "session_id": "source-session",
        "trigger": "opencode-compacting",
        "project_slug": "unknown",
        "project_root": "",
        "occurred_at": "2026-07-31T12:34:56+00:00",
        "enqueued_by": "flush_memory",
    }
    payload.update(overrides)
    return payload


def _run_cli(memory_queue, monkeypatch, capsys, *args, stdin=""):
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", *args])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = memory_queue._cli()
    captured = capsys.readouterr()
    return code, captured


@pytest.mark.parametrize("option", ("--apply-sdk-result", "--renew-sdk-task"))
def test_sdk_cli_rejects_oversized_stdin_without_mutating_lease(
    clean_queue,
    monkeypatch,
    capsys,
    option,
):
    class BoundedOnlyInput(io.StringIO):
        def __init__(self, value: str):
            super().__init__(value)
            self.request_sizes: list[int] = []

        def read(self, size: int = -1) -> str:
            self.request_sizes.append(size)
            assert size > 0, "reader requested an unbounded allocation"
            return super().read(size)

    task_id = clean_queue.enqueue("query", {"prompt": "preserve lease"})
    prepared = clean_queue.prepare_sdk_task()
    lease_path = clean_queue._queue_dir() / f"{task_id}.processing"
    original_lease = lease_path.read_bytes()
    payload = {
        **prepared,
        "success": True,
        "response": "answer",
        "padding": "x" * 512,
    }
    stream = BoundedOnlyInput(json.dumps(payload))
    monkeypatch.setattr(clean_queue, "MAX_SDK_BRIDGE_STDIN_BYTES", 128, raising=False)
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", option])
    monkeypatch.setattr(sys, "stdin", stream)

    code = clean_queue._cli()
    captured = capsys.readouterr()

    assert code == 2
    assert "invalid" in captured.err.lower()
    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert lease_path.read_bytes() == original_lease
    assert not (clean_queue._queue_dir().parent / "queue-results").exists()


def test_sdk_cli_prepares_and_applies_one_query(clean_queue, monkeypatch, capsys):
    first_id = clean_queue.enqueue("query", {"prompt": "first", "system_prompt": "rules"})
    clean_queue.enqueue("query", {"prompt": "second"})

    code, captured = _run_cli(
        clean_queue, monkeypatch, capsys, "--prepare-sdk-task"
    )

    assert code == 0
    prepared = json.loads(captured.out)
    assert prepared["pending"] is True
    assert prepared["task_id"] == first_id
    assert prepared["prompt"] == "first"
    assert prepared["system_prompt"] == "rules"
    assert prepared["lease_id"]
    assert len(prepared["digest"]) == 64
    assert "recover_project_root" not in prepared
    assert "source_session_id" not in prepared
    assert len(list(clean_queue._queue_dir().glob("*.processing"))) == 1

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "--apply-sdk-result",
        stdin=json.dumps({**prepared, "success": True, "response": "answer"}),
    )

    assert code == 0, captured.err
    result_path = clean_queue._queue_dir().parent / "queue-results" / f"{first_id}.txt"
    assert result_path.read_text(encoding="utf-8") == "answer"
    assert [task["payload"]["prompt"] for task in clean_queue.list_pending()] == ["second"]


def test_sdk_apply_rederives_result_type_from_digest_covered_task(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)

    query_id = clean_queue.enqueue("query", {"prompt": "answer the query"})
    query_prepared = clean_queue.prepare_sdk_task()
    query_lease_path = clean_queue._queue_dir() / f"{query_id}.processing"
    query_lease = json.loads(query_lease_path.read_text(encoding="utf-8"))
    query_lease["_sdk_lease"]["result_type"] = "flush"
    assert clean_queue._task_digest(query_lease) == query_prepared["digest"]
    clean_queue._atomic_write_json(query_lease_path, query_lease)
    query_result = clean_queue.apply_sdk_result(
        {**query_prepared, "success": True, "response": "query answer"}
    )

    flush_id = clean_queue.enqueue("flush", {"prompt": "classify legacy flush"})
    flush_prepared = clean_queue.prepare_sdk_task()
    flush_lease_path = clean_queue._queue_dir() / f"{flush_id}.processing"
    flush_lease = json.loads(flush_lease_path.read_text(encoding="utf-8"))
    flush_lease["_sdk_lease"]["result_type"] = "query"
    assert clean_queue._task_digest(flush_lease) == flush_prepared["digest"]
    clean_queue._atomic_write_json(flush_lease_path, flush_lease)
    flush_result = clean_queue.apply_sdk_result(
        {**flush_prepared, "success": True, "response": "FLUSH_OK"}
    )

    results_dir = clean_queue._queue_dir().parent / "queue-results"
    query_output = results_dir / f"{query_id}.txt"
    flush_output = results_dir / f"{flush_id}.txt"
    assert query_result == (True, "acknowledged")
    assert flush_result == (True, "acknowledged")
    assert (
        query_output.exists(),
        flush_output.exists(),
        daily_dir.exists(),
    ) == (True, False, False)
    assert query_output.read_text(encoding="utf-8") == "query answer"
    assert clean_queue.list_pending() == []


def test_prepare_exposes_recovery_only_for_unversioned_transitional_flush(clean_queue):
    transitional_id = clean_queue.enqueue("flush", _transitional_flush_payload())

    prepared = clean_queue.prepare_sdk_task()

    assert prepared["task_id"] == transitional_id
    assert prepared["recover_project_root"] is True
    assert prepared["source_session_id"] == "source-session"
    lease_path = clean_queue._queue_dir() / f"{transitional_id}.processing"
    leased = json.loads(lease_path.read_text(encoding="utf-8"))
    assert "recover_project_root" not in leased["payload"]
    assert "source_session_id" not in leased["payload"]
    assert "recover_project_root" not in leased["_sdk_lease"]
    assert "source_session_id" not in leased["_sdk_lease"]
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": False, "error": "lookup unavailable"}
    ) == (True, "failure recorded")
    [retained] = clean_queue.list_pending()
    assert retained["id"] == transitional_id
    assert retained["attempts"] == 1
    assert retained["last_error"] == "lookup unavailable"

    non_recovery_tasks = [
        ("query", {"prompt": "normal query"}),
        (
            "flush",
            {
                "prompt": "legacy flush",
                "event": "pre-compact",
                "enqueued_by": "flush_memory",
            },
        ),
        (
            "flush",
            {
                "prompt": "persisted unversioned flush",
                "project_slug": "alpha",
                "project_root": "D:/projects/alpha",
            },
        ),
        (
            "flush",
            {
                "prompt": "persisted version one flush",
                "provenance_version": 1,
                "event": "session-end",
                "session_id": "versioned-session",
                "trigger": "stop",
                "project_slug": "alpha",
                "project_root": "D:/projects/alpha",
                "occurred_at": "2026-07-31T12:34:56Z",
                "enqueued_by": "flush_memory",
            },
        ),
    ]
    for task_type, payload in non_recovery_tasks:
        task_id = clean_queue.enqueue(task_type, payload)
        non_recovery = clean_queue.prepare_sdk_task()
        assert non_recovery["task_id"] == task_id
        assert "recover_project_root" not in non_recovery
        assert "source_session_id" not in non_recovery
        assert clean_queue.apply_sdk_result(
            {**non_recovery, "success": False, "error": "test settlement"}
        ) == (True, "failure recorded")


def test_prepare_rejects_malformed_flush_provenance_before_provider_work(clean_queue):
    malformed_payloads = [
        {
            "prompt": "unsupported version",
            "provenance_version": 2,
            "event": "pre-compact",
            "session_id": "source-session",
            "trigger": "opencode-compacting",
            "project_slug": "alpha",
            "project_root": "D:/projects/alpha",
            "occurred_at": "2026-07-31T12:34:56+00:00",
            "enqueued_by": "flush_memory",
        },
        {
            "prompt": "version one missing identity",
            "provenance_version": 1,
            "event": "pre-compact",
            "session_id": "source-session",
            "trigger": "opencode-compacting",
            "project_slug": "unknown",
            "project_root": "",
            "occurred_at": "2026-07-31T12:34:56+00:00",
            "enqueued_by": "flush_memory",
        },
        {
            "prompt": "conflicting identity",
            "event": "pre-compact",
            "session_id": "source-session",
            "trigger": "opencode-compacting",
            "project_slug": "alpha",
            "project_root": "",
            "occurred_at": "2026-07-31T12:34:56+00:00",
            "enqueued_by": "flush_memory",
        },
        {
            "prompt": "incomplete transitional provenance",
            "session_id": "source-session",
        },
    ]
    task_ids = [clean_queue.enqueue("flush", payload) for payload in malformed_payloads]

    assert clean_queue.prepare_sdk_task() == {"pending": False}

    pending = clean_queue.list_pending()
    assert [task["id"] for task in pending] == task_ids
    assert all(task["attempts"] == 1 for task in pending)
    assert all("invalid flush provenance" in task["last_error"] for task in pending)
    assert not list(clean_queue._queue_dir().glob("*.processing"))


def test_recovered_flush_applies_confirmed_canonical_identity(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    import flush_memory

    daily_dir = tmp_path / "knowledge" / "daily"
    source_root = (tmp_path / "source-project").resolve()
    canonical_root = (tmp_path / "canonical-project").resolve()
    source_root.mkdir()
    canonical_root.mkdir()
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    calls = []

    def resolve(slug, root, *, env=None):
        calls.append((slug, root, env))
        return "canonical-source", canonical_root

    monkeypatch.setattr(flush_memory, "_resolve_project_identity", resolve)
    task_id = clean_queue.enqueue("flush", _transitional_flush_payload())
    payload_before = clean_queue.list_pending()[0]["payload"].copy()
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {
            **prepared,
            "success": True,
            "response": "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Recover safely.",
            "recovered_project_root": str(source_root),
        }
    ) == (True, "acknowledged")

    assert calls == [(None, str(source_root), {})]
    assert clean_queue.list_pending() == []
    assert not (clean_queue._queue_dir() / f"{task_id}.processing").exists()
    text = (daily_dir / "2026-07-31.md").read_text(encoding="utf-8")
    assert "Project slug: `canonical-source`" in text
    assert f"Project root JSON: {json.dumps(str(canonical_root))}" in text
    assert json.dumps(str(source_root)) not in text
    assert "Recover safely." in text
    assert payload_before["project_slug"] == "unknown"
    assert payload_before["project_root"] == ""
    assert "recovered_project_root" not in payload_before

    ok_task_id = clean_queue.enqueue("flush", _transitional_flush_payload())
    ok_prepared = clean_queue.prepare_sdk_task()
    assert ok_prepared["task_id"] == ok_task_id
    assert clean_queue.apply_sdk_result(
        {
            **ok_prepared,
            "success": True,
            "response": "FLUSH_OK",
            "recovered_project_root": str(source_root),
        }
    ) == (True, "acknowledged")
    assert calls == [
        (None, str(source_root), {}),
        (None, str(source_root), {}),
    ]
    assert clean_queue.list_pending() == []
    assert not (clean_queue._queue_dir() / f"{ok_task_id}.processing").exists()
    assert (daily_dir / "2026-07-31.md").read_text(encoding="utf-8") == text

    missing_root_id = clean_queue.enqueue("flush", _transitional_flush_payload())
    missing_root = clean_queue.prepare_sdk_task()
    assert missing_root["task_id"] == missing_root_id
    calls_before_missing_root = calls.copy()
    assert clean_queue.apply_sdk_result(
        {
            **missing_root,
            "success": True,
            "response": "FLUSH_OK",
        }
    ) == (True, "failure recorded")
    assert calls == calls_before_missing_root
    [retained] = clean_queue.list_pending()
    assert retained["id"] == missing_root_id
    assert retained["attempts"] == 1
    assert retained["last_error"] == (
        "invalid SDK result: recovered_project_root is required"
    )
    assert not list(clean_queue._queue_dir().glob("*.processing"))
    assert (daily_dir / "2026-07-31.md").read_text(encoding="utf-8") == text


def test_recovered_root_failures_retain_task_without_daily_write(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    import flush_memory

    daily_dir = tmp_path / "knowledge" / "daily"
    unconfirmed = (tmp_path / "unconfirmed").resolve()
    nonexistent = (tmp_path / "missing").resolve()
    short_root_path = (tmp_path / "short-source").resolve()
    unconfirmed.mkdir()
    short_root_path.mkdir()
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    resolver_calls = []
    oversized_supplied_root = "C:/" + "x" * (
        clean_queue.MAX_PROVENANCE_CHARS + 1 - len("C:/")
    )
    oversized_canonical_root = Path(
        "C:/"
        + "y" * (clean_queue.MAX_PROVENANCE_CHARS + 1 - len("C:/"))
    )
    short_root = str(short_root_path)
    assert len(oversized_supplied_root) == clean_queue.MAX_PROVENANCE_CHARS + 1
    assert len(str(oversized_canonical_root)) == clean_queue.MAX_PROVENANCE_CHARS + 1
    assert len(short_root) <= clean_queue.MAX_PROVENANCE_CHARS

    def unresolved(slug, root, *, env=None):
        resolver_calls.append((slug, root, env))
        if root == oversized_supplied_root:
            return "canonical-source", unconfirmed
        if root == short_root:
            return "canonical-source", oversized_canonical_root
        return None

    monkeypatch.setattr(flush_memory, "_resolve_project_identity", unresolved)
    cases = [
        ("missing", {}),
        ("none", {"recovered_project_root": None}),
        ("integer", {"recovered_project_root": 7}),
        ("blank", {"recovered_project_root": ""}),
        ("padded", {"recovered_project_root": f" {unconfirmed}"}),
        ("relative", {"recovered_project_root": "relative/project"}),
        ("control", {"recovered_project_root": "C:/project\0bad"}),
        (
            "oversized",
            {"recovered_project_root": oversized_supplied_root},
        ),
        ("nonexistent", {"recovered_project_root": str(nonexistent)}),
        ("unconfirmed", {"recovered_project_root": str(unconfirmed)}),
        ("oversized-canonical", {"recovered_project_root": short_root}),
    ]

    task_ids = []
    for label, extra in cases:
        task_id = clean_queue.enqueue(
            "flush",
            _transitional_flush_payload(prompt=f"classify {label}"),
        )
        task_ids.append(task_id)
        prepared = clean_queue.prepare_sdk_task()
        assert prepared["task_id"] == task_id
        outcome = clean_queue.apply_sdk_result(
            {
                **prepared,
                "success": True,
                "response": "FLUSH_MINOR\n\n**Open questions**\n- Retry later.",
                **extra,
            }
        )
        assert outcome == (True, "failure recorded"), label

    failed_root = str(unconfirmed)
    failed_task_id = clean_queue.enqueue(
        "flush",
        _transitional_flush_payload(prompt="classify failed result with root"),
    )
    task_ids.append(failed_task_id)
    cases.append(("failed-result", {"recovered_project_root": failed_root}))
    failed_prepared = clean_queue.prepare_sdk_task()
    assert failed_prepared["task_id"] == failed_task_id
    assert clean_queue.apply_sdk_result(
        {
            **failed_prepared,
            "success": False,
            "error": "provider failed after lookup",
            "recovered_project_root": failed_root,
        }
    ) == (True, "failure recorded")

    pending = clean_queue.list_pending()
    assert [task["id"] for task in pending] == task_ids
    assert all(task["attempts"] == 1 for task in pending)
    assert all("recovered_project_root" not in task for task in pending)
    assert all("recovered_project_root" not in task["payload"] for task in pending)
    assert all("_sdk_lease" not in task for task in pending)
    for task, (_label, extra) in zip(pending, cases, strict=True):
        candidate = extra.get("recovered_project_root")
        if isinstance(candidate, str) and candidate:
            assert candidate not in task["last_error"]
    assert pending[-1]["last_error"] == (
        "invalid SDK result: failed results cannot include recovered_project_root"
    )
    canonical_failure = next(
        task
        for task in pending
        if task["payload"]["prompt"] == "classify oversized-canonical"
    )
    assert canonical_failure["last_error"] == (
        "apply failed: ValueError: flush task project identity is unavailable"
    )
    assert str(oversized_canonical_root) not in canonical_failure["last_error"]
    assert resolver_calls == [
        (None, "relative/project", {}),
        (None, str(nonexistent), {}),
        (None, str(unconfirmed), {}),
        (None, short_root, {}),
    ]
    assert not daily_dir.exists()
    assert not list(clean_queue._queue_dir().glob("*.processing"))
    assert not list(clean_queue._queue_dir().glob("*.acquiring"))


def test_transient_root_is_forbidden_outside_recovery_shape(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "knowledge" / "daily"
    project_root = (tmp_path / "project").resolve()
    project_root.mkdir()
    root = str(project_root)
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    normal_shapes = [
        ("query", {"prompt": "answer"}, "answer"),
        ("compile", {"force": True}, "COMPILE_COMPLETED"),
        ("flush", {"prompt": "legacy"}, "FLUSH_OK"),
        (
            "flush",
            {
                "prompt": "persisted",
                "project_slug": "alpha",
                "project_root": root,
            },
            "FLUSH_OK",
        ),
        (
            "flush",
            {
                **_transitional_flush_payload(
                    prompt="versioned",
                    project_slug="alpha",
                    project_root=root,
                ),
                "provenance_version": 1,
            },
            "FLUSH_OK",
        ),
    ]

    task_ids = []
    for task_type, payload, response in normal_shapes:
        task_id = clean_queue.enqueue(task_type, payload)
        task_ids.append(task_id)
        prepared = clean_queue.prepare_sdk_task()
        assert prepared["task_id"] == task_id
        assert clean_queue.apply_sdk_result(
            {
                **prepared,
                "success": True,
                "response": response,
                "recovered_project_root": root,
            }
        ) == (True, "failure recorded")

    pending = clean_queue.list_pending()
    assert [task["id"] for task in pending] == task_ids
    assert all(task["attempts"] == 1 for task in pending)
    assert all("recovered_project_root" not in task for task in pending)
    assert all(root not in task["last_error"] for task in pending)
    assert not list(clean_queue._queue_dir().glob("*.processing"))
    assert not (clean_queue._queue_dir().parent / "queue-results").exists()
    assert not daily_dir.exists()


def test_recovered_flush_retry_is_idempotent_by_durable_task_id(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    import flush_memory

    daily_dir = tmp_path / "knowledge" / "daily"
    source_root = (tmp_path / "source-project").resolve()
    source_root.mkdir()
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("source", source_root)
            if slug is None and root == str(source_root) and env == {}
            else None
        ),
    )
    clean_queue.enqueue("flush", _transitional_flush_payload())
    prepared = clean_queue.prepare_sdk_task()
    lease_path = clean_queue._queue_dir() / f"{prepared['task_id']}.processing"
    leased = json.loads(lease_path.read_text(encoding="utf-8"))
    response = "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Append once."

    clean_queue.apply_classified_flush_response(
        leased,
        response,
        recovered_project_root=str(source_root),
    )
    assert clean_queue.apply_sdk_result(
        {
            **prepared,
            "success": True,
            "response": response,
            "recovered_project_root": str(source_root),
        }
    ) == (True, "acknowledged")

    text = (daily_dir / "2026-07-31.md").read_text(encoding="utf-8")
    assert text.count("Append once.") == 1
    assert text.count("<!-- llm-wiki-queue-task:") == 1
    assert leased["payload"]["project_slug"] == "unknown"
    assert leased["payload"]["project_root"] == ""
    assert "recovered_project_root" not in leased["payload"]


def test_malformed_versioned_flush_ok_is_not_acknowledged(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    task_id = clean_queue.enqueue(
        "flush",
        {
            **_transitional_flush_payload(),
            "provenance_version": 1,
        },
    )
    task, _lease_path = clean_queue._claim_next_task()
    lease = task["_sdk_lease"]

    assert clean_queue.apply_sdk_result(
        {
            "task_id": task_id,
            "lease_id": lease["id"],
            "digest": lease["digest"],
            "success": True,
            "response": "FLUSH_OK",
        }
    ) == (True, "failure recorded")

    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["attempts"] == 1
    assert "invalid flush provenance" in pending["last_error"]
    assert not list(clean_queue._queue_dir().glob("*.processing"))
    assert not daily_dir.exists()


def test_sdk_cli_rejects_stale_lease_response_without_acknowledging(
    clean_queue, monkeypatch, capsys
):
    clean_queue.enqueue("query", {"prompt": "preserve me"})
    _, captured = _run_cli(clean_queue, monkeypatch, capsys, "--prepare-sdk-task")
    stale = json.loads(captured.out)
    assert clean_queue.recover_stale_leases(max_age_seconds=-1) == 1
    _, captured = _run_cli(clean_queue, monkeypatch, capsys, "--prepare-sdk-task")
    current = json.loads(captured.out)
    assert current["lease_id"] != stale["lease_id"]
    lease_path = clean_queue._queue_dir() / f"{current['task_id']}.processing"
    original_lease = lease_path.read_bytes()

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "--apply-sdk-result",
        stdin=json.dumps(
            {
                **stale,
                "success": True,
                "response": "stale",
                "recovered_project_root": None,
            }
        ),
    )

    assert code != 0
    assert "stale" in captured.err.lower()
    assert len(list(clean_queue._queue_dir().glob("*.processing"))) == 1
    assert lease_path.read_bytes() == original_lease
    assert not (clean_queue._queue_dir().parent / "queue-results").exists()


def test_sdk_cli_rejects_changed_task_digest(clean_queue, monkeypatch, capsys):
    clean_queue.enqueue("query", {"prompt": "preserve me"})
    _, captured = _run_cli(clean_queue, monkeypatch, capsys, "--prepare-sdk-task")
    prepared = json.loads(captured.out)
    lease_path = clean_queue._queue_dir() / f"{prepared['task_id']}.processing"
    original_lease = lease_path.read_bytes()

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "--apply-sdk-result",
        stdin=json.dumps(
            {
                **prepared,
                "digest": "0" * 64,
                "success": True,
                "response": "changed",
                "recovered_project_root": None,
            }
        ),
    )

    assert code != 0
    assert "digest" in captured.err.lower()
    assert len(list(clean_queue._queue_dir().glob("*.processing"))) == 1
    assert lease_path.read_bytes() == original_lease
    assert not (clean_queue._queue_dir().parent / "queue-results").exists()


def test_sdk_cli_provider_failure_persists_attempt_and_releases_lease(
    clean_queue, monkeypatch, capsys
):
    clean_queue.enqueue("flush", {"prompt": "classify", "event": "idle"})
    _, captured = _run_cli(clean_queue, monkeypatch, capsys, "--prepare-sdk-task")
    prepared = json.loads(captured.out)

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "--apply-sdk-result",
        stdin=json.dumps(
            {**prepared, "success": False, "error": "provider unavailable"}
        ),
    )

    assert code == 0, captured.err
    [pending] = clean_queue.list_pending()
    assert pending["attempts"] == 1
    assert pending["last_attempt_at"]
    assert pending["last_error"] == "provider unavailable"
    assert not list(clean_queue._queue_dir().glob("*.processing"))


def test_sdk_prepare_skips_exhausted_legacy_tasks(
    clean_queue, monkeypatch, capsys
):
    exhausted = clean_queue.enqueue("query", {"prompt": "exhausted"})
    for _ in range(5):
        clean_queue.mark_attempt(exhausted, success=False)
    ready = clean_queue.enqueue("query", {"prompt": "ready"})

    code, captured = _run_cli(clean_queue, monkeypatch, capsys, "--prepare-sdk-task")

    assert code == 0
    assert json.loads(captured.out)["task_id"] == ready


def test_stale_recovery_waits_for_sdk_apply_settlement(clean_queue, monkeypatch):
    marker_observed = []
    original_write = clean_queue._atomic_write_json

    def tracked_write(path, data):
        if path.suffix == ".processing":
            marker_observed.append(path.with_suffix(".acquiring").exists())
        original_write(path, data)

    monkeypatch.setattr(clean_queue, "_atomic_write_json", tracked_write)
    clean_queue.enqueue("query", {"prompt": "race-safe"})
    prepared = clean_queue.prepare_sdk_task()
    assert marker_observed == [True]
    assert not list(clean_queue._queue_dir().glob("*.acquiring"))

    apply_entered = threading.Event()
    release_apply = threading.Event()
    recovery_started = threading.Event()
    recovery_finished = threading.Event()
    side_effects = []
    apply_results = []
    recovery_counts = []

    def paused_apply(_task, response):
        side_effects.append(response)
        apply_entered.set()
        assert release_apply.wait(timeout=2)

    def apply_result():
        apply_results.append(
            clean_queue.apply_sdk_result(
                {**prepared, "success": True, "response": "exactly once"}
            )
        )

    def recover_while_apply_is_paused():
        recovery_started.set()
        recovery_counts.append(clean_queue.recover_stale_leases(max_age_seconds=0))
        recovery_finished.set()

    monkeypatch.setattr(clean_queue, "_apply_sdk_response", paused_apply)
    apply_thread = threading.Thread(target=apply_result)
    apply_thread.start()
    assert apply_entered.wait(timeout=1)

    recovery_thread = threading.Thread(target=recover_while_apply_is_paused)
    recovery_thread.start()
    assert recovery_started.wait(timeout=1)
    recovery_was_blocked = not recovery_finished.wait(timeout=0.2)
    recovery_was_alive = recovery_thread.is_alive()

    release_apply.set()
    apply_thread.join(timeout=2)
    recovery_thread.join(timeout=2)

    assert recovery_was_blocked
    assert recovery_was_alive
    assert not apply_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert side_effects == ["exactly once"]
    assert apply_results == [(True, "acknowledged")]
    assert recovery_counts == [0]
    assert clean_queue.list_pending() == []
    assert not list(clean_queue._queue_dir().glob("*.processing"))
    assert not list(clean_queue._queue_dir().glob("*.acquiring"))


@pytest.mark.parametrize(
    ("task_type", "payload", "result_type"),
    [
        ("classify", {"prompt": "classify", "event": "idle"}, "flush"),
        ("classify", {"prompt": "classify"}, "query"),
        ("lint_contradictions", {"prompt": "inspect contradictions"}, "query"),
    ],
)
def test_sdk_prepare_migrates_legacy_prompt_tasks(
    clean_queue, task_type, payload, result_type
):
    clean_queue.enqueue(task_type, payload)

    prepared = clean_queue.prepare_sdk_task()

    assert prepared["pending"] is True
    assert prepared["type"] == task_type
    assert prepared["result_type"] == result_type
    ok, status = clean_queue.apply_sdk_result(
        {
            **prepared,
            "success": True,
            "response": "FLUSH_OK" if result_type == "flush" else "legacy result",
        }
    )
    assert (ok, status) == (True, "acknowledged")
    assert clean_queue.list_pending() == []


def test_sdk_prepare_marks_unserviceable_contradiction_terminal(clean_queue):
    clean_queue.enqueue("lint_contradictions", {"daily": "missing-prompt.md"})

    assert clean_queue.prepare_sdk_task() == {"pending": False}

    [pending] = clean_queue.list_pending()
    assert pending["attempts"] == clean_queue.MAX_ATTEMPTS
    assert pending["terminal_failure"] is True
    assert "missing prompt" in pending["last_error"]


def test_sdk_compile_control_is_acknowledged_only_by_compile_completion(
    clean_queue, monkeypatch
):
    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", lambda: False)
    task_id = clean_queue.enqueue("compile", {"force": True})
    prepared = clean_queue.prepare_sdk_task()

    assert prepared["kind"] == "compile"
    ok, status = clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": "COMPILE_COMPLETED"}
    )

    assert ok is True
    assert status == "acknowledged"
    assert not (clean_queue._queue_dir() / f"{task_id}.processing").exists()


def test_held_compile_control_returns_to_pending_after_plugin_state_loss(clean_queue):
    task_id = clean_queue.enqueue("compile", {"force": True})
    held = clean_queue.prepare_sdk_task()

    assert held["kind"] == "compile"
    assert held["task_id"] == task_id
    assert (clean_queue._queue_dir() / f"{task_id}.processing").exists()
    assert clean_queue.list_pending() == []

    assert clean_queue.recover_stale_leases(max_age_seconds=-1) == 1
    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["type"] == "compile"
    assert pending["attempts"] == 0

    resumed = clean_queue.prepare_sdk_task()
    assert resumed["task_id"] == task_id
    assert resumed["lease_id"] != held["lease_id"]


def test_sdk_and_manual_flush_apply_use_shared_helper_with_equivalent_blocks(
    clean_queue, tmp_path, monkeypatch, capsys
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    import flush_memory

    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("alpha", Path(root).resolve())
            if slug == "alpha" and root and env == {}
            else None
        ),
    )
    payload = {
        "prompt": "classify",
        "event": "pre-compact",
        "session_id": "session-1",
        "trigger": "opencode-compacting",
        "project_slug": "alpha",
        "project_root": "D:/projects/alpha",
        "occurred_at": "2026-07-27T12:34:56+00:00",
    }
    response = "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Keep queue provenance."
    helper_calls = []
    original_apply = clean_queue.apply_classified_flush_response

    def tracked_apply(task, result):
        helper_calls.append(task["id"])
        return original_apply(task, result)

    monkeypatch.setattr(clean_queue, "apply_classified_flush_response", tracked_apply)

    clean_queue.enqueue("flush", payload)
    prepared = clean_queue.prepare_sdk_task()
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": response}
    ) == (True, "acknowledged")
    daily = daily_dir / "2026-07-27.md"
    sdk_text = daily.read_text(encoding="utf-8")
    daily.unlink()

    clean_queue.enqueue("flush", payload)
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", response)
    code, captured = _run_cli(clean_queue, monkeypatch, capsys, "drain")
    assert code == 0, captured.err
    manual_text = daily.read_text(encoding="utf-8")

    marker = r"<!-- llm-wiki-queue-task: [0-9a-f]{64} -->"
    assert re.sub(marker, "<!-- marker -->", sdk_text) == re.sub(
        marker, "<!-- marker -->", manual_text
    )
    queue_marker = re.search(marker, sdk_text)
    assert queue_marker is not None
    assert sdk_text.index("Keep queue provenance.") < queue_marker.start()
    assert len(helper_calls) == 2
    assert len(set(helper_calls)) == 2
    for expected in (
        "## [12:34:56] deferred-pre-compact | session-1",
        "- Trigger: `opencode-compacting`",
        "- Project slug: `alpha`",
        f"- Project root JSON: {json.dumps(str(Path('D:/projects/alpha').resolve()))}",
        "- Tier: `minor`",
        "- Source session: `session-1`",
    ):
        assert expected in sdk_text


def test_flush_provenance_boundary_accepts_legacy_and_routes_complete_transitional_recovery(
    clean_queue, tmp_path, monkeypatch
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    clean_queue.enqueue(
        "flush",
        {
            "prompt": "classify",
            "system_prompt": "classify a legacy transcript",
            "max_tokens": 1500,
            "enqueued_by": "flush_memory",
            "event": "pre-compact",
        },
    )
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {
            **prepared,
            "success": True,
            "response": "FLUSH_MINOR\n\n**Open questions**\n- What remains?",
        }
    ) == (True, "acknowledged")
    daily_files = list(daily_dir.glob("*.md"))
    assert len(daily_files) == 1
    daily_path = daily_files[0]
    text = daily_path.read_text(encoding="utf-8")
    assert "deferred-pre-compact | unknown" in text
    assert "- Trigger: `unknown`" in text
    assert "- Project slug: `unknown`" in text
    assert '- Project root JSON: "unknown"' in text
    assert "- Source session: `unknown`" in text

    current_task_id = clean_queue.enqueue(
        "flush",
        _transitional_flush_payload(
            prompt="classify",
            session_id="partial-current-provenance",
        ),
    )
    current = clean_queue.prepare_sdk_task()

    assert current["task_id"] == current_task_id
    assert current["recover_project_root"] is True
    assert current["source_session_id"] == "partial-current-provenance"
    assert clean_queue.apply_sdk_result(
        {
            **current,
            "success": True,
            "response": "FLUSH_MINOR\n\n**Open questions**\n- Must this fail closed?",
        }
    ) == (True, "failure recorded")
    [pending] = clean_queue.list_pending()
    assert pending["id"] == current_task_id
    assert pending["attempts"] == 1
    assert "recovered_project_root is required" in pending["last_error"]
    assert daily_path.read_text(encoding="utf-8") == text


def test_reapplying_persisted_flush_task_is_idempotent_by_durable_task_id(
    clean_queue, tmp_path, monkeypatch
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    import flush_memory

    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("alpha", Path(root).resolve())
            if slug == "alpha" and root and env == {}
            else None
        ),
    )
    clean_queue.enqueue(
        "flush",
        {
            "prompt": "classify",
            "event": "session-end",
            "session_id": "session-1",
            "trigger": "stop",
            "project_slug": "alpha",
            "project_root": "D:/projects/alpha",
            "occurred_at": "2026-07-27T12:34:56",
        },
    )
    prepared = clean_queue.prepare_sdk_task()
    lease_path = clean_queue._queue_dir() / f"{prepared['task_id']}.processing"
    leased_task = json.loads(lease_path.read_text(encoding="utf-8"))
    response = "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Write once across retries."

    clean_queue.apply_classified_flush_response(leased_task, response)
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": response}
    ) == (True, "acknowledged")

    text = (daily_dir / "2026-07-27.md").read_text(encoding="utf-8")
    assert text.count("Write once across retries.") == 1
    assert text.count("<!-- llm-wiki-queue-task:") == 1


def test_locked_append_once_requires_canonical_marker_before_creating_directory(
    tmp_path,
):
    import daily_log_append

    daily = tmp_path / "daily" / "2026-07-28.md"

    with pytest.raises(ValueError, match="canonical queue task marker"):
        daily_log_append.locked_append_once(
            daily,
            "content\n<!-- llm-wiki-queue-task: short -->\n",
            "<!-- llm-wiki-queue-task: short -->",
            state_root=tmp_path / "runtime",
        )

    assert not daily.parent.exists()


def test_locked_append_once_ignores_marker_in_unrelated_daily(
    tmp_path,
):
    import daily_log_append

    daily = tmp_path / "daily" / "2026-07-28.md"
    daily.parent.mkdir()
    marker = f"<!-- llm-wiki-queue-task: {'a' * 64} -->"
    unrelated = daily.with_name("2026-07-27.md")
    original = f"unrelated\n{marker}\n"
    unrelated.write_text(original, encoding="utf-8")

    assert daily_log_append.locked_append_once(
        daily,
        f"target write\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == daily
    assert "target write" in daily.read_text(encoding="utf-8")
    assert unrelated.read_text(encoding="utf-8") == original


def test_capture_marker_is_global_across_daily_dates(tmp_path):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    first = daily_dir / "2026-08-01.md"
    second = daily_dir / "2026-08-02.md"
    marker = f"<!-- llm-wiki-capture: {'a' * 64} -->"

    assert daily_log_append.locked_append_once(
        first,
        f"first capture\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == first
    assert daily_log_append.locked_append_once(
        second,
        f"duplicate after midnight\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == first

    assert first.read_text(encoding="utf-8").count(marker) == 1
    assert not second.exists()


@pytest.mark.parametrize(
    "unsafe_candidate",
    ("oversized", "unreadable", "symlink", "reparse"),
)
def test_capture_global_scan_fails_closed_on_unsafe_daily_candidate(
    tmp_path,
    monkeypatch,
    unsafe_candidate,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    candidate = daily_dir / "2026-08-01.md"
    target = daily_dir / "2026-08-02.md"
    marker = f"<!-- llm-wiki-capture: {'b' * 64} -->"
    outside = tmp_path / "outside.md"
    outside.write_text("outside must remain untouched\n", encoding="utf-8")
    candidate.write_text("prior safe daily\n", encoding="utf-8")

    if unsafe_candidate == "oversized":
        monkeypatch.setattr(
            daily_log_append,
            "MAX_DAILY_MARKER_SCAN_BYTES",
            8,
            raising=False,
        )
    elif unsafe_candidate == "unreadable":
        real_open = daily_log_append._open_daily_candidate_descriptor

        def denied_open(path, directory_bound):
            if path == candidate:
                raise PermissionError("injected unreadable daily")
            return real_open(path, directory_bound)

        monkeypatch.setattr(
            daily_log_append,
            "_open_daily_candidate_descriptor",
            denied_open,
        )
    elif unsafe_candidate == "symlink":
        candidate.unlink()
        try:
            candidate.symlink_to(outside)
        except OSError:
            pytest.skip("file symlinks are not available on this platform")
    else:
        reparse_flag = getattr(
            daily_log_append.stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
        real_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, metadata):
                self._metadata = metadata
                self.st_mode = metadata.st_mode
                self.st_size = metadata.st_size
                self.st_file_attributes = reparse_flag

            def __getattr__(self, name):
                return getattr(self._metadata, name)

        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path: ReparseMetadata(real_lstat(path))
            if path == candidate
            else real_lstat(path),
        )

    with pytest.raises(RuntimeError, match="daily marker scan"):
        daily_log_append.locked_append_once(
            target,
            f"must not append\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert not target.exists()
    assert outside.read_text(encoding="utf-8") == "outside must remain untouched\n"


def test_capture_global_scan_bounds_daily_inventory(tmp_path, monkeypatch):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    for day in ("2026-08-01", "2026-08-02"):
        (daily_dir / f"{day}.md").write_text("safe daily\n", encoding="utf-8")
    target = daily_dir / "2026-08-03.md"
    marker = f"<!-- llm-wiki-capture: {'c' * 64} -->"
    monkeypatch.setattr(
        daily_log_append,
        "MAX_DAILY_MARKER_SCAN_FILES",
        1,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="daily marker scan.*limit"):
        daily_log_append.locked_append_once(
            target,
            f"must not append\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert not target.exists()


@pytest.mark.parametrize("unsafe_directory", ("symlink", "reparse"))
def test_capture_global_scan_rejects_unsafe_daily_directory(
    tmp_path,
    monkeypatch,
    unsafe_directory,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe_directory == "symlink":
        try:
            daily_dir.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are not available on this platform")
    else:
        daily_dir.mkdir()
        reparse_flag = getattr(
            daily_log_append.stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
        real_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, metadata):
                self._metadata = metadata
                self.st_mode = metadata.st_mode
                self.st_size = metadata.st_size
                self.st_dev = metadata.st_dev
                self.st_ino = metadata.st_ino
                self.st_file_attributes = reparse_flag

            def __getattr__(self, name):
                return getattr(self._metadata, name)

        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path: ReparseMetadata(real_lstat(path))
            if path == daily_dir
            else real_lstat(path),
        )

    target = daily_dir / "2026-08-03.md"
    marker = f"<!-- llm-wiki-capture: {'f' * 64} -->"
    with pytest.raises((OSError, RuntimeError), match="director|marker scan"):
        daily_log_append.locked_append_once(
            target,
            f"must not append\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert not (outside / target.name).exists()


def test_capture_global_scan_rejects_candidate_replaced_before_fd_open(
    tmp_path,
    monkeypatch,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    candidate = daily_dir / "2026-08-01.md"
    replacement = daily_dir / "replacement.tmp"
    target = daily_dir / "2026-08-02.md"
    candidate.write_text("original identity\n", encoding="utf-8")
    replacement.write_text("replacement identity\n", encoding="utf-8")
    marker = f"<!-- llm-wiki-capture: {'1' * 64} -->"
    real_open = daily_log_append._open_daily_candidate_descriptor
    swapped = False

    def swapping_open(path, directory_bound):
        nonlocal swapped
        if not swapped and Path(path).name == candidate.name:
            swapped = True
            os.replace(replacement, candidate)
        return real_open(path, directory_bound)

    monkeypatch.setattr(
        daily_log_append,
        "_open_daily_candidate_descriptor",
        swapping_open,
    )

    with pytest.raises(RuntimeError, match="daily marker scan"):
        daily_log_append.locked_append_once(
            target,
            f"must not append\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert swapped is True
    assert not target.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_capture_global_scan_path_swap_to_fifo_never_blocks(tmp_path, monkeypatch):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    candidate = daily_dir / "2026-08-01.md"
    target = daily_dir / "2026-08-02.md"
    candidate.write_text("regular before open\n", encoding="utf-8")
    marker = f"<!-- llm-wiki-capture: {'2' * 64} -->"
    real_open = daily_log_append._open_daily_candidate_descriptor
    swapped = False

    def fifo_swapping_open(path, directory_bound):
        nonlocal swapped
        if not swapped and Path(path).name == candidate.name:
            swapped = True
            candidate.unlink()
            os.mkfifo(candidate)
        return real_open(path, directory_bound)

    monkeypatch.setattr(
        daily_log_append,
        "_open_daily_candidate_descriptor",
        fifo_swapping_open,
    )

    with pytest.raises(RuntimeError, match="daily marker scan"):
        daily_log_append.locked_append_once(
            target,
            f"must not append\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert swapped is True
    assert not target.exists()


def test_capture_global_scan_reads_candidate_by_descriptor(tmp_path, monkeypatch):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    candidate = daily_dir / "2026-08-01.md"
    target = daily_dir / "2026-08-02.md"
    marker = f"<!-- llm-wiki-capture: {'3' * 64} -->"
    candidate.write_text(f"prior capture\n{marker}\n", encoding="utf-8")
    real_path_open = Path.open

    def denied_path_open(path, *args, **kwargs):
        if path == candidate:
            raise AssertionError("daily candidates must be read by descriptor")
        return real_path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_path_open)

    assert daily_log_append.locked_append_once(
        target,
        f"duplicate\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == candidate
    assert not target.exists()


def test_capture_inventory_rejects_casefold_target_alias(tmp_path):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    target = daily_dir / "2026-08-01.md"
    collision = daily_dir / "2026-08-01.MD"
    original = b"case-preserved daily must survive\n"
    collision.write_bytes(original)
    marker = f"<!-- llm-wiki-capture: {'9' * 64} -->"

    with pytest.raises(RuntimeError, match="case-insensitive.*collision"):
        daily_log_append.locked_append_once(
            target,
            f"new capture\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert collision.read_bytes() == original


def test_capture_inventory_rejects_distinct_casefold_collision(tmp_path):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    target = daily_dir / "2026-08-01.md"
    collision = daily_dir / "2026-08-01.MD"
    target_original = b"canonical daily\n"
    collision_original = b"case-colliding daily\n"
    target.write_bytes(target_original)
    try:
        descriptor = os.open(
            collision,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        )
    except FileExistsError:
        pytest.skip("filesystem treats casefold-equivalent names as one entry")
    try:
        os.write(descriptor, collision_original)
    finally:
        os.close(descriptor)
    marker = f"<!-- llm-wiki-capture: {'a' * 64} -->"

    with pytest.raises(RuntimeError, match="case-insensitive.*collision"):
        daily_log_append.locked_append_once(
            target,
            f"new capture\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert target.read_bytes() == target_original
    assert collision.read_bytes() == collision_original


@pytest.mark.parametrize("standalone", (False, True), ids=("inline", "standalone"))
def test_capture_global_scan_requires_standalone_marker_line(
    tmp_path,
    monkeypatch,
    standalone,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    existing = daily_dir / "2026-08-01.md"
    target = daily_dir / "2026-08-02.md"
    marker = f"<!-- llm-wiki-capture: {'b' * 64} -->"
    prior = f"{marker}\n" if standalone else f"inline metadata text {marker} remains prose\n"
    existing.write_text(prior, encoding="utf-8")
    monkeypatch.setattr(
        daily_log_append,
        "DAILY_MARKER_SCAN_CHUNK_BYTES",
        7,
        raising=False,
    )

    result = daily_log_append.locked_append_once(
        target,
        f"new capture\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    )

    if standalone:
        assert result == existing
        assert not target.exists()
    else:
        assert result == target
        assert target.read_text(encoding="utf-8").splitlines().count(marker) == 1
    assert existing.read_text(encoding="utf-8") == prior


@pytest.mark.parametrize("swap_kind", ("hardlink", "symlink", "replacement"))
def test_capture_append_reuses_scanned_target_bytes_after_path_swap(
    tmp_path,
    monkeypatch,
    swap_kind,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-08-01.md"
    outside = tmp_path / "outside.md"
    original = "validated target bytes\n"
    outside_original = "outside must remain untouched\n"
    replacement = tmp_path / "replacement.md"
    daily.write_text(original, encoding="utf-8")
    outside.write_text(outside_original, encoding="utf-8")
    replacement.write_text("replacement must not be adopted\n", encoding="utf-8")
    marker = f"<!-- llm-wiki-capture: {'6' * 64} -->"
    appended = f"new capture\n{marker}\n"

    if swap_kind == "symlink":
        probe = tmp_path / "symlink-probe"
        try:
            probe.symlink_to(outside)
        except OSError:
            pytest.skip("file symlinks are not available on this platform")
        probe.unlink()

    real_open = daily_log_append._open_daily_candidate_descriptor
    real_close = os.close
    target_descriptor: int | None = None
    swapped = False

    def tracking_open(path, directory_bound):
        nonlocal target_descriptor
        descriptor = real_open(path, directory_bound)
        if path == daily:
            target_descriptor = descriptor
        return descriptor

    def swapping_close(descriptor):
        nonlocal swapped
        real_close(descriptor)
        if descriptor != target_descriptor or swapped:
            return
        swapped = True
        daily.unlink()
        if swap_kind == "hardlink":
            os.link(outside, daily)
        elif swap_kind == "symlink":
            daily.symlink_to(outside)
        else:
            os.replace(replacement, daily)

    monkeypatch.setattr(
        daily_log_append,
        "_open_daily_candidate_descriptor",
        tracking_open,
    )
    monkeypatch.setattr(os, "close", swapping_close)

    try:
        result = daily_log_append.locked_append_once(
            daily,
            appended,
            marker,
            state_root=tmp_path / "runtime",
        )
    except OSError:
        if swap_kind != "symlink":
            raise
        result = None

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == outside_original
    if result is not None:
        assert result == daily
        assert daily.read_text(encoding="utf-8") == original + appended
        assert not os.path.samefile(daily, outside)


@pytest.mark.parametrize("extra_byte", (False, True))
def test_capture_global_scan_accepts_exact_file_limit_only(
    tmp_path,
    extra_byte,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    candidate = daily_dir / "2026-08-01.md"
    existing = daily_dir / "2026-08-02.md"
    target = daily_dir / "2026-08-03.md"
    marker = f"<!-- llm-wiki-capture: {'7' * 64} -->"
    candidate.write_bytes(
        b"x" * (daily_log_append.MAX_DAILY_MARKER_SCAN_BYTES + int(extra_byte))
    )
    existing.write_text(f"prior capture\n{marker}\n", encoding="utf-8")

    if extra_byte:
        with pytest.raises(RuntimeError, match="daily marker scan.*byte limit"):
            daily_log_append.locked_append_once(
                target,
                f"duplicate\n{marker}\n",
                marker,
                state_root=tmp_path / "runtime",
            )
    else:
        assert daily_log_append.locked_append_once(
            target,
            f"duplicate\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        ) == existing

    assert candidate.stat().st_size == (
        daily_log_append.MAX_DAILY_MARKER_SCAN_BYTES + int(extra_byte)
    )
    assert not target.exists()


def test_capture_append_rejects_final_content_beyond_scan_limit(
    tmp_path,
    monkeypatch,
):
    import daily_log_append

    daily = tmp_path / "daily" / "2026-08-01.md"
    daily.parent.mkdir()
    marker = f"<!-- llm-wiki-capture: {'8' * 64} -->"
    original = b"x" * 200
    daily.write_bytes(original)
    monkeypatch.setattr(
        daily_log_append,
        "MAX_DAILY_MARKER_SCAN_BYTES",
        256,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="daily.*byte limit"):
        daily_log_append.locked_append_once(
            daily,
            f"new capture\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert daily.read_bytes() == original


def test_capture_global_scan_stops_before_later_unsafe_candidate(
    tmp_path,
    monkeypatch,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    first = daily_dir / "2026-08-01.md"
    later = daily_dir / "2026-08-02.md"
    target = daily_dir / "2026-08-03.md"
    marker = f"<!-- llm-wiki-capture: {'4' * 64} -->"
    first.write_text(f"prior capture\n{marker}\n", encoding="utf-8")
    later.write_bytes(b"x" * 1_024)
    monkeypatch.setattr(
        daily_log_append,
        "MAX_DAILY_MARKER_SCAN_BYTES",
        256,
        raising=False,
    )

    assert daily_log_append.locked_append_once(
        target,
        f"duplicate\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == first
    assert not target.exists()


def test_capture_global_scan_enforces_aggregate_byte_limit(tmp_path, monkeypatch):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    for day in ("2026-08-01", "2026-08-02"):
        (daily_dir / f"{day}.md").write_bytes(b"x" * 10)
    target = daily_dir / "2026-08-03.md"
    marker = f"<!-- llm-wiki-capture: {'5' * 64} -->"
    monkeypatch.setattr(
        daily_log_append,
        "MAX_DAILY_MARKER_SCAN_TOTAL_BYTES",
        15,
        raising=False,
    )
    real_read = os.read
    bytes_read = 0

    def tracking_read(descriptor, size):
        nonlocal bytes_read
        chunk = real_read(descriptor, size)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", tracking_read)

    with pytest.raises(RuntimeError, match="daily marker scan.*aggregate"):
        daily_log_append.locked_append_once(
            target,
            f"must not append\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    assert daily_log_append.MAX_DAILY_MARKER_SCAN_FILES == 4_096
    assert bytes_read == 15
    assert not target.exists()


@pytest.mark.parametrize("marker_kind", ("queue-task", "direct-flush", "capture"))
def test_locked_append_once_accepts_canonical_marker_families(
    tmp_path,
    marker_kind: str,
):
    import daily_log_append

    daily = tmp_path / "daily" / "2026-07-28.md"
    marker = f"<!-- llm-wiki-{marker_kind}: {'d' * 64} -->"

    assert daily_log_append.locked_append_once(
        daily,
        f"new block\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == daily
    assert daily.read_text(encoding="utf-8").count(marker) == 1


def test_daily_append_capture_id_must_match_block_marker(tmp_path):
    import flush_memory

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = (tmp_path / "project").resolve()
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    marker = f"<!-- llm-wiki-capture: {'d' * 64} -->"
    _day, block = flush_memory.render_flush_block(
        "minor",
        "CAPTURE_ID_MISMATCH_MUST_NOT_APPEND",
        event="opencode-idle",
        session_id="session-1",
        trigger="opencode-idle",
        project_slug="project",
        project_root=str(project),
        occurred_at="2026-08-01T12:00:00+00:00",
        idempotency_marker=marker,
    )
    payload = {
        "slug": "project",
        "projectRoot": str(project),
        "sessionId": "session-1",
        "captureId": "e" * 64,
        "block": block,
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "daily_log_append.py")],
        cwd=SCRIPTS_DIR.parent,
        env={
            **os.environ,
            "LLM_WIKI_ROOT": str(vault),
            "LLM_WIKI_STATE_ROOT": str(state_root),
        },
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert not (vault / "knowledge" / "daily").exists()


@pytest.mark.parametrize(
    "case",
    (
        "canonical-plus-malformed",
        "canonical-plus-hidden-malformed",
        "malformed-without-capture-id",
        "duplicate-canonical",
        "missing-canonical",
    ),
)
def test_daily_append_rejects_invalid_capture_prefix_contract(tmp_path, case):
    import flush_memory

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = (tmp_path / "project").resolve()
    (project / ".git").mkdir(parents=True)
    template = vault / "knowledge" / "projects" / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    capture_id = "d" * 64
    canonical = f"<!-- llm-wiki-capture: {capture_id} -->"
    malformed = "<!-- llm-wiki-capture: short -->"
    marker_lines = {
        "canonical-plus-malformed": [canonical, malformed],
        "canonical-plus-hidden-malformed": [canonical, f"- quoted {malformed}"],
        "malformed-without-capture-id": [malformed],
        "duplicate-canonical": [canonical, canonical],
        "missing-canonical": [],
    }[case]
    _day, block = flush_memory.render_flush_block(
        "minor",
        "**Gotchas / debugging**\n- Reject malformed capture metadata.",
        event="opencode-idle",
        session_id="session-1",
        trigger="opencode-idle",
        project_slug="project",
        project_root=str(project),
        occurred_at="2026-08-01T12:00:00+00:00",
    )
    block = block.replace(
        "<!-- llm-wiki-record-complete -->",
        "\n".join([*marker_lines, "<!-- llm-wiki-record-complete -->"]),
    )
    payload = {
        "slug": "project",
        "projectRoot": str(project),
        "sessionId": "session-1",
        "block": block,
    }
    if case != "malformed-without-capture-id":
        payload["captureId"] = capture_id

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "daily_log_append.py")],
        cwd=SCRIPTS_DIR.parent,
        env={
            **os.environ,
            "LLM_WIKI_ROOT": str(vault),
            "LLM_WIKI_STATE_ROOT": str(state_root),
        },
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert not (vault / "knowledge" / "daily").exists()


def test_direct_then_queue_apply_uses_one_capture_marker(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    import daily_log_append
    import flush_memory

    daily_dir = tmp_path / "knowledge" / "daily"
    project_root = (tmp_path / "alpha").resolve()
    project_root.mkdir()
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("alpha", project_root)
            if slug == "alpha" and root == str(project_root) and env == {}
            else None
        ),
    )
    payload = flush_memory._build_flush_queue_payload(
        "user: durable capture",
        "session-end",
        session_id="session-1",
        trigger="hook",
        project_slug="alpha",
        project_root=str(project_root),
        occurred_at="2026-08-01T12:00:00+00:00",
        project_identity_confirmed=True,
    )
    assert payload is not None
    marker = f"<!-- llm-wiki-capture: {payload['capture_id']} -->"
    response = "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Append one capture."
    tier, body = flush_memory._classify_response(response)
    day, block = flush_memory.render_flush_block(
        tier,
        body,
        event="session-end",
        session_id="session-1",
        trigger="hook",
        project_slug="alpha",
        project_root=str(project_root),
        occurred_at="2026-08-01T12:00:00+00:00",
        idempotency_marker=marker,
    )
    daily = daily_dir / f"{day}.md"
    daily_log_append.locked_append_once(
        daily,
        block,
        marker,
        state_root=tmp_path / "runtime",
    )

    task_id = clean_queue.enqueue("flush", payload)
    [task] = clean_queue.list_pending()
    assert task["id"] == task_id
    clean_queue.apply_classified_flush_response(task, response)

    text = daily.read_text(encoding="utf-8")
    assert text.count("Append one capture.") == 1
    assert text.count(marker) == 1


def test_ack_then_reenqueue_same_capture_dedupes_daily_apply(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    import flush_memory

    daily_dir = tmp_path / "knowledge" / "daily"
    project_root = (tmp_path / "alpha").resolve()
    project_root.mkdir()
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("alpha", project_root)
            if slug == "alpha" and root == str(project_root) and env == {}
            else None
        ),
    )
    payload = {
        "prompt": "classify captured transcript",
        "capture_id": "d" * 64,
        "event": "session-end",
        "session_id": "session-1",
        "trigger": "hook",
        "project_slug": "alpha",
        "project_root": str(project_root),
        "occurred_at": "2026-08-01T12:00:00+00:00",
        "enqueued_by": "flush_memory",
        "provenance_version": 1,
    }
    response = "FLUSH_MINOR\n\n**Open questions**\n- Apply after ack once."

    first = clean_queue.enqueue("flush", payload)
    prepared = clean_queue.prepare_sdk_task()
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": response}
    ) == (True, "acknowledged")

    second = clean_queue.enqueue("flush", payload)
    assert second != first
    prepared = clean_queue.prepare_sdk_task()
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": response}
    ) == (True, "acknowledged")

    marker = f"<!-- llm-wiki-capture: {payload['capture_id']} -->"
    text = (daily_dir / "2026-08-01.md").read_text(encoding="utf-8")
    assert text.count("Apply after ack once.") == 1
    assert text.count(marker) == 1


@pytest.mark.parametrize(
    "unsafe_candidate",
    ("oversized", "unreadable", "symlink", "reparse"),
)
def test_locked_append_once_fails_closed_on_uncertain_target(
    tmp_path,
    monkeypatch,
    unsafe_candidate: str,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    candidate = daily
    marker = f"<!-- llm-wiki-queue-task: {'b' * 64} -->"
    outside = tmp_path / "outside.md"
    outside.write_text("outside must remain untouched\n", encoding="utf-8")
    candidate.write_text("prior daily without marker\n", encoding="utf-8")

    if unsafe_candidate == "oversized":
        monkeypatch.setattr(
            daily_log_append,
            "MAX_DAILY_MARKER_SCAN_BYTES",
            8,
            raising=False,
        )
    elif unsafe_candidate == "unreadable":
        real_open = daily_log_append._open_daily_candidate_descriptor

        def denied_open(path, directory_bound):
            if path == candidate:
                raise PermissionError("injected unreadable daily")
            return real_open(path, directory_bound)

        monkeypatch.setattr(
            daily_log_append,
            "_open_daily_candidate_descriptor",
            denied_open,
        )
    elif unsafe_candidate == "symlink":
        candidate.unlink()
        try:
            candidate.symlink_to(outside)
        except OSError:
            pytest.skip("file symlinks are not available on this platform")
    else:
        reparse_flag = getattr(daily_log_append.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        real_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, metadata):
                self._metadata = metadata
                self.st_mode = metadata.st_mode
                self.st_size = metadata.st_size
                self.st_file_attributes = reparse_flag

            def __getattr__(self, name):
                return getattr(self._metadata, name)

        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path: ReparseMetadata(real_lstat(path))
            if path == candidate
            else real_lstat(path),
        )

    with pytest.raises(RuntimeError, match="daily marker scan"):
        daily_log_append.locked_append_once(
            daily,
            f"new block\n{marker}\n",
            marker,
            state_root=tmp_path / "runtime",
        )

    if unsafe_candidate == "symlink":
        assert daily.is_symlink()
    else:
        assert daily.read_text(encoding="utf-8") == "prior daily without marker\n"
    assert outside.read_text(encoding="utf-8") == "outside must remain untouched\n"


@pytest.mark.parametrize(
    "unsafe_candidate",
    ("oversized", "unreadable", "symlink", "reparse"),
)
def test_locked_append_once_ignores_uncertain_unrelated_daily(
    tmp_path,
    monkeypatch,
    unsafe_candidate: str,
):
    import daily_log_append

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    daily = daily_dir / "2026-07-28.md"
    candidate = daily_dir / "2026-07-27.md"
    marker = f"<!-- llm-wiki-queue-task: {'c' * 64} -->"
    outside = tmp_path / "outside.md"
    outside.write_text("outside must remain untouched\n", encoding="utf-8")
    candidate.write_text("prior daily without marker\n", encoding="utf-8")

    if unsafe_candidate == "oversized":
        monkeypatch.setattr(
            daily_log_append,
            "MAX_DAILY_MARKER_SCAN_BYTES",
            8,
            raising=False,
        )
    elif unsafe_candidate == "unreadable":
        real_open = daily_log_append._open_daily_candidate_descriptor

        def denied_open(path, directory_bound):
            if path == candidate:
                raise PermissionError("injected unreadable daily")
            return real_open(path, directory_bound)

        monkeypatch.setattr(
            daily_log_append,
            "_open_daily_candidate_descriptor",
            denied_open,
        )
    elif unsafe_candidate == "symlink":
        candidate.unlink()
        try:
            candidate.symlink_to(outside)
        except OSError:
            pytest.skip("file symlinks are not available on this platform")
    else:
        reparse_flag = getattr(daily_log_append.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        real_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, metadata):
                self._metadata = metadata
                self.st_mode = metadata.st_mode
                self.st_size = metadata.st_size
                self.st_file_attributes = reparse_flag

            def __getattr__(self, name):
                return getattr(self._metadata, name)

        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path: ReparseMetadata(real_lstat(path))
            if path == candidate
            else real_lstat(path),
        )

    assert daily_log_append.locked_append_once(
        daily,
        f"new block\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == daily

    assert "new block" in daily.read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "outside must remain untouched\n"


def test_locked_append_once_scans_marker_with_bounded_chunk_overlap(
    tmp_path,
    monkeypatch,
):
    import daily_log_append

    daily = tmp_path / "daily" / "2026-07-28.md"
    daily.parent.mkdir()
    marker = f"<!-- llm-wiki-queue-task: {'e' * 64} -->"
    daily.write_bytes(b"1234567" + marker.encode("ascii") + b"\n")
    monkeypatch.setattr(
        daily_log_append,
        "DAILY_MARKER_SCAN_CHUNK_BYTES",
        8,
        raising=False,
    )
    real_open = daily_log_append._open_daily_candidate_descriptor
    real_read = os.read
    read_sizes: list[int] = []
    candidate_descriptor: int | None = None

    def tracking_open(path, directory_bound):
        nonlocal candidate_descriptor
        descriptor = real_open(path, directory_bound)
        if path == daily:
            candidate_descriptor = descriptor
        return descriptor

    def tracking_read(descriptor, size):
        if descriptor == candidate_descriptor:
            read_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(
        daily_log_append,
        "_open_daily_candidate_descriptor",
        tracking_open,
    )
    monkeypatch.setattr(os, "read", tracking_read)

    assert daily_log_append.locked_append_once(
        daily,
        f"duplicate\n{marker}\n",
        marker,
        state_root=tmp_path / "runtime",
    ) == daily
    assert read_sizes
    assert all(0 < size <= daily_log_append.DAILY_MARKER_SCAN_CHUNK_BYTES for size in read_sizes)


def test_unrelated_oversized_daily_does_not_block_queue_acknowledgement(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    import daily_log_append
    import flush_memory

    daily_dir = tmp_path / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-07-26.md").write_text("oversized", encoding="utf-8")
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    monkeypatch.setattr(daily_log_append, "MAX_DAILY_MARKER_SCAN_BYTES", 4, raising=False)
    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("alpha", Path(root).resolve())
            if slug == "alpha" and root and env == {}
            else None
        ),
    )
    task_id = clean_queue.enqueue(
        "flush",
        {
            "prompt": "classify",
            "project_slug": "alpha",
            "project_root": "D:/projects/alpha",
            "occurred_at": "2026-07-28T12:34:56",
        },
    )
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {
            **prepared,
            "success": True,
            "response": "FLUSH_MINOR\n\n**Gotchas / debugging**\n- Retry safely.",
        }
    ) == (True, "acknowledged")

    assert clean_queue.list_pending() == []
    assert not (clean_queue._queue_dir() / f"{task_id}.processing").exists()
    assert "Retry safely" in (daily_dir / "2026-07-28.md").read_text(encoding="utf-8")


def test_invalid_occurred_at_falls_back_inside_daily_dir_and_redacts_metadata(
    clean_queue, tmp_path, monkeypatch
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    import flush_memory

    monkeypatch.setattr(
        flush_memory,
        "_resolve_project_identity",
        lambda slug, root, *, env=None: (
            ("alpha", Path(root).resolve())
            if slug == "alpha" and root and env == {}
            else None
        ),
    )
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    project_root = str((tmp_path / "projects" / "alpha").resolve())
    clean_queue.enqueue(
        "flush",
        {
            "prompt": "classify",
            "event": "session-end",
            "session_id": "session-1",
            "trigger": f"token={secret}",
            "project_slug": "alpha",
            "project_root": project_root,
            "occurred_at": "../../escape-daily",
        },
    )
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {
            **prepared,
            "success": True,
            "response": "FLUSH_MINOR\n\n**Open questions**\n- Safe fallback?",
        }
    ) == (True, "acknowledged")

    expected = daily_dir / f"{datetime.now():%Y-%m-%d}.md"
    assert expected.exists()
    assert list(daily_dir.glob("*.md")) == [expected]
    assert not (tmp_path / "escape-daily.md").exists()
    text = expected.read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text


def test_flush_ok_acknowledges_without_writing(clean_queue, tmp_path, monkeypatch):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    clean_queue.enqueue("flush", {"prompt": "classify"})
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": "FLUSH_OK"}
    ) == (True, "acknowledged")
    assert not daily_dir.exists()


def test_near_miss_flush_ok_remains_pending_as_apply_failure(
    clean_queue, tmp_path, monkeypatch
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    clean_queue.enqueue("flush", {"prompt": "classify"})
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": "FLUSH_OK."}
    ) == (True, "failure recorded")

    [pending] = clean_queue.list_pending()
    assert pending["attempts"] == 1
    assert "exact FLUSH_OK token" in pending["last_error"]
    assert not daily_dir.exists()


def test_non_ok_flush_without_body_remains_pending_as_apply_failure(
    clean_queue, tmp_path, monkeypatch
):
    daily_dir = tmp_path / "knowledge" / "daily"
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    clean_queue.enqueue("flush", {"prompt": "classify"})
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": "FLUSH_MAJOR"}
    ) == (True, "failure recorded")

    [pending] = clean_queue.list_pending()
    assert pending["attempts"] == 1
    assert "no distilled body" in pending["last_error"]
    assert not daily_dir.exists()


def test_sdk_prepare_prefers_eligible_noncompile_over_older_compile(clean_queue):
    compile_id = clean_queue.enqueue("compile", {"force": True})
    query_id = clean_queue.enqueue("query", {"prompt": "later query"})

    prepared = clean_queue.prepare_sdk_task()

    assert prepared["task_id"] == query_id
    assert prepared["kind"] == "sdk"
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": "query result"}
    ) == (True, "acknowledged")
    compile_task = clean_queue.prepare_sdk_task()
    assert compile_task["task_id"] == compile_id
    assert compile_task["kind"] == "compile"


def test_sdk_and_manual_claims_rename_under_queue_order_lock(
    clean_queue, monkeypatch
):
    original_lock = clean_queue._queue_order_lock
    original_rename = Path.rename
    lock_depth = 0
    observed_depths = []

    @contextmanager
    def tracked_lock():
        nonlocal lock_depth
        with original_lock() as queue_dir:
            lock_depth += 1
            try:
                yield queue_dir
            finally:
                lock_depth -= 1

    def tracked_rename(path, target):
        if path.suffix == ".json" and Path(target).suffix == ".processing":
            observed_depths.append(lock_depth)
        return original_rename(path, target)

    monkeypatch.setattr(clean_queue, "_queue_order_lock", tracked_lock)
    monkeypatch.setattr(Path, "rename", tracked_rename)

    clean_queue.enqueue("query", {"prompt": "sdk"})
    prepared = clean_queue.prepare_sdk_task()
    assert clean_queue.apply_sdk_result(
        {**prepared, "success": True, "response": "sdk result"}
    ) == (True, "acknowledged")

    clean_queue.enqueue("query", {"prompt": "manual"})
    assert clean_queue.drain_with(lambda _task: True, max_tasks=1)["ok"] == 1

    assert observed_depths == [1, 1]


def test_manual_drain_processes_noncompile_before_synchronous_compile(
    clean_queue, monkeypatch, capsys
):
    import llm_client
    import maybe_compile

    events = []
    clean_queue.enqueue("compile", {"force": True})
    clean_queue.enqueue("query", {"prompt": "query work"})

    def fake_call_llm(prompt, _system_prompt, *, max_tokens):
        events.append(("query", prompt, max_tokens))
        return "query result"

    def fake_run(command, **kwargs):
        events.append(("compile", command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(llm_client, "call_llm", fake_call_llm)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", lambda: False)
    monkeypatch.setattr(
        maybe_compile,
        "spawn_compile_if_idle",
        lambda **_kwargs: events.append(("detached",)) or (True, "spawned"),
    )

    code, captured = _run_cli(clean_queue, monkeypatch, capsys, "drain")

    assert code == 0, captured.err
    assert [event[0] for event in events] == ["query", "compile"]
    command = events[1][1]
    assert command == [
        sys.executable,
        str(SCRIPTS_DIR / "compile_memory.py"),
        "--trigger",
        "manual",
    ]
    assert clean_queue.list_pending() == []


def test_manual_compile_success_stays_pending_when_compile_work_remains(
    clean_queue, monkeypatch
):
    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", lambda: True)
    task_id = clean_queue.enqueue("compile", {"force": True})
    processed = []

    counts = clean_queue.drain_with(
        lambda task: processed.append(task["id"]) or True
    )

    assert counts == {"ok": 0, "failed": 0, "skipped": 0, "pending": 1}
    assert processed == [task_id]
    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["attempts"] == 0
    assert "_sdk_lease" not in pending
    assert not list(clean_queue._queue_dir().glob("*.processing"))


def test_manual_compile_settlement_serializes_with_daily_append(
    clean_queue, tmp_path, monkeypatch
):
    from daily_log_append import locked_append

    daily_dir = tmp_path / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-27.md"
    daily.write_text("## compiled snapshot\n", encoding="utf-8")
    (tmp_path / "knowledge" / "index.md").write_text("# Index\n", encoding="utf-8")
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    digest = clean_queue.hashlib.sha256(daily.read_bytes()).hexdigest()
    generation = "a" * 64
    state_file = clean_queue._queue_dir().parent / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "compiled_daily_hashes": {
                    daily.name: digest
                },
                "compiled_daily_receipts": {
                    daily.name: {
                        "version": 1,
                        "daily_sha256": digest,
                        "generation_id": generation,
                        "journal_ids": [],
                        "effects": [],
                        "targets": [],
                        "index": {
                            "generation_id": generation,
                            "entries": [],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    original_id = clean_queue.enqueue("compile", {"force": True})
    check_finished = threading.Event()
    release_check = threading.Event()
    append_started = threading.Event()
    append_finished = threading.Event()
    drain_results = []
    ensure_results = []
    errors = []
    real_check = clean_queue._has_pending_compile_work

    def paused_check():
        result = real_check()
        check_finished.set()
        if not release_check.wait(timeout=5):
            raise TimeoutError("test did not release compile-work check")
        return result

    def drain():
        try:
            drain_results.append(
                clean_queue.drain_with(lambda _task: True, max_tasks=1)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def append_and_ensure():
        append_started.set()
        try:
            locked_append(
                daily,
                "\n## appended after compile check\n",
                state_root=tmp_path,
            )
            ensure_results.append(clean_queue.ensure_compile_task())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            append_finished.set()

    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", paused_check)
    settlement = threading.Thread(target=drain)
    settlement.start()
    assert check_finished.wait(timeout=5)
    appender = threading.Thread(target=append_and_ensure)
    appender.start()
    assert append_started.wait(timeout=5)
    assert not append_finished.wait(timeout=0.2)

    release_check.set()
    settlement.join(timeout=5)
    appender.join(timeout=5)

    assert not settlement.is_alive()
    assert not appender.is_alive()
    assert errors == []
    assert drain_results == [
        {"ok": 1, "failed": 0, "skipped": 0, "pending": 0}
    ]
    assert ensure_results[0]["created"] is True
    [pending] = clean_queue.list_pending()
    assert pending["type"] == "compile"
    assert pending["id"] != original_id


def test_manual_drain_keeps_compile_pending_when_synchronous_compile_fails(
    clean_queue, monkeypatch, capsys
):
    import maybe_compile

    clean_queue.enqueue("compile", {"force": True})
    compile_calls = []

    def fake_run(command, **kwargs):
        compile_calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        maybe_compile,
        "spawn_compile_if_idle",
        lambda **_kwargs: (True, "detached launch must not acknowledge"),
    )

    code, captured = _run_cli(clean_queue, monkeypatch, capsys, "drain")

    assert code == 0, captured.err
    assert len(compile_calls) == 1
    [pending] = clean_queue.list_pending()
    assert pending["type"] == "compile"
    assert pending["attempts"] == 1


def test_manual_drain_cap_counts_only_claimed_eligible_tasks(clean_queue):
    for index in range(3):
        task_id = clean_queue.enqueue("query", {"prompt": f"terminal-{index}"})
        for _ in range(clean_queue.MAX_ATTEMPTS):
            clean_queue.mark_attempt(task_id, success=False)
    backed_off = clean_queue.enqueue("query", {"prompt": "backoff"})
    clean_queue.mark_attempt(backed_off, success=False)
    clean_queue.enqueue("query", {"prompt": "eligible-0"})
    clean_queue.enqueue("query", {"prompt": "eligible-1"})

    seen = []
    counts = clean_queue.drain_with(
        lambda task: seen.append(task["payload"]["prompt"]) or True,
        max_tasks=2,
    )

    assert seen == ["eligible-0", "eligible-1"]
    assert counts["ok"] == 2


def test_capped_compile_control_is_identity_validated_and_requeued_without_attempt(
    clean_queue
):
    task_id = clean_queue.enqueue("compile", {"force": True})
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result({**prepared, "defer": True}) == (
        True,
        "deferred",
    )

    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["attempts"] == 0
    assert pending["last_attempt_at"] is None
    assert "_sdk_lease" not in pending
    assert not list(clean_queue._queue_dir().glob("*.processing"))


def test_concurrent_sdk_applies_run_side_effect_once_and_do_not_recreate_task(
    clean_queue, monkeypatch
):
    clean_queue.enqueue("query", {"prompt": "apply once"})
    prepared = clean_queue.prepare_sdk_task()
    start = threading.Barrier(3)
    counter_lock = threading.Lock()
    first_entered = threading.Event()
    second_entered = threading.Event()
    calls = 0
    active = 0
    max_active = 0
    outcomes = []

    def tracked_apply(_task, _response):
        nonlocal calls, active, max_active
        with counter_lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        if call_number == 1:
            first_entered.set()
            second_entered.wait(timeout=0.2)
        else:
            second_entered.set()
        with counter_lock:
            active -= 1

    def apply_result():
        start.wait()
        outcome = clean_queue.apply_sdk_result(
            {**prepared, "success": True, "response": "result"}
        )
        with counter_lock:
            outcomes.append(outcome)

    monkeypatch.setattr(clean_queue, "_apply_sdk_response", tracked_apply)
    threads = [threading.Thread(target=apply_result) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    assert first_entered.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=2)

    assert not [thread for thread in threads if thread.is_alive()]
    assert calls == 1
    assert max_active == 1
    assert outcomes.count((True, "acknowledged")) == 1
    assert len([outcome for outcome in outcomes if outcome[0] is False]) == 1
    assert clean_queue.list_pending() == []
    assert not list(clean_queue._queue_dir().glob("*.processing"))


def test_enqueue_sanitizes_bounded_one_line_provenance_before_persistence(
    clean_queue
):
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    payload = {
        "prompt": "classify this transcript",
        "event": "pre-compact\nforged-event" + ("x" * 800),
        "session_id": f"session\r\ntoken={secret}",
        "trigger": f"authorization: bearer {secret}\nforged-trigger",
        "project_slug": "alpha\nforged-slug",
        "project_root": f"D:/projects/{secret}/alpha\nforged-root",
        "occurred_at": "2026-07-27T12:34:56+00:00\nforged-time",
        "enqueued_by": "flush_memory\nforged-source",
    }

    task_id = clean_queue.enqueue("flush", payload)

    persisted = json.loads(
        (clean_queue._queue_dir() / f"{task_id}.json").read_text(encoding="utf-8")
    )["payload"]
    for field in clean_queue.PROVENANCE_FIELDS:
        assert "\n" not in persisted[field]
        assert "\r" not in persisted[field]
        assert len(persisted[field]) <= clean_queue.MAX_PROVENANCE_CHARS
    assert secret not in json.dumps(persisted, ensure_ascii=False)
    assert "REDACTED" in persisted["trigger"]
    assert payload["session_id"] == f"session\r\ntoken={secret}"


def test_concurrent_ensure_compile_task_creates_one_durable_control(
    clean_queue, monkeypatch
):
    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", lambda: True)
    workers = 12
    barrier = threading.Barrier(workers)
    results = []

    def ensure():
        barrier.wait()
        results.append(clean_queue.ensure_compile_task())

    threads = [threading.Thread(target=ensure) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not [thread for thread in threads if thread.is_alive()]
    assert sum(result["created"] for result in results) == 1
    assert len({result["task_id"] for result in results}) == 1
    [control] = clean_queue.list_pending()
    assert control["type"] == "compile"
    assert control["enqueue_sequence"] == 1


def test_ensure_compile_task_reuses_processing_control(clean_queue, monkeypatch):
    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", lambda: True)
    first = clean_queue.ensure_compile_task()
    leased = clean_queue.prepare_sdk_task()

    ensured = clean_queue.ensure_compile_task()

    assert first["created"] is True
    assert leased["task_id"] == first["task_id"]
    assert ensured == {
        "pending": True,
        "created": False,
        "task_id": first["task_id"],
        "state": "processing",
    }
    assert len(list(clean_queue._queue_dir().glob("*.processing"))) == 1
    assert not list(clean_queue._queue_dir().glob("*.json"))


def test_ensure_compile_task_skips_when_compile_state_has_no_work(
    clean_queue, monkeypatch
):
    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", lambda: False)

    assert clean_queue.ensure_compile_task() == {
        "pending": False,
        "created": False,
        "task_id": None,
        "state": "not_needed",
    }
    assert clean_queue.list_pending() == []


def test_compile_work_check_distrusts_matching_hash_without_receipt(
    clean_queue,
    tmp_path,
    monkeypatch,
):
    daily_dir = tmp_path / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-27.md"
    daily.write_text("compiled bytes without receipt", encoding="utf-8")
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    state_file = clean_queue._queue_dir().parent / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "compiled_daily_hashes": {
                    daily.name: clean_queue.hashlib.sha256(
                        daily.read_bytes()
                    ).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )

    assert clean_queue._has_pending_compile_work() is True


def test_compile_settlement_serializes_check_and_delete_with_daily_append(
    clean_queue, tmp_path, monkeypatch
):
    from daily_log_append import locked_append

    daily_dir = tmp_path / "knowledge" / "daily"
    daily_dir.mkdir(parents=True)
    daily = daily_dir / "2026-07-27.md"
    daily.write_text("## compiled snapshot\n", encoding="utf-8")
    (tmp_path / "knowledge" / "index.md").write_text("# Index\n", encoding="utf-8")
    monkeypatch.setattr(clean_queue, "_daily_dir", lambda: daily_dir)
    digest = clean_queue.hashlib.sha256(daily.read_bytes()).hexdigest()
    generation = "b" * 64
    state_file = clean_queue._queue_dir().parent / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "compiled_daily_hashes": {
                    daily.name: digest
                },
                "compiled_daily_receipts": {
                    daily.name: {
                        "version": 1,
                        "daily_sha256": digest,
                        "generation_id": generation,
                        "journal_ids": [],
                        "effects": [],
                        "targets": [],
                        "index": {
                            "generation_id": generation,
                            "entries": [],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    task_id = clean_queue.enqueue("compile", {"force": True})
    prepared = clean_queue.prepare_sdk_task()
    check_finished = threading.Event()
    release_check = threading.Event()
    append_started = threading.Event()
    append_finished = threading.Event()
    apply_results = []
    errors = []
    real_check = clean_queue._has_pending_compile_work

    def paused_check():
        result = real_check()
        check_finished.set()
        if not release_check.wait(timeout=5):
            raise TimeoutError("test did not release compile-work check")
        return result

    def settle():
        try:
            apply_results.append(
                clean_queue.apply_sdk_result(
                    {**prepared, "success": True, "response": "COMPILE_COMPLETED"}
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def append():
        append_started.set()
        try:
            locked_append(
                daily,
                "\n## appended after compile check\n",
                state_root=tmp_path,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            append_finished.set()

    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", paused_check)
    settlement = threading.Thread(target=settle)
    settlement.start()
    assert check_finished.wait(timeout=5)
    appender = threading.Thread(target=append)
    appender.start()
    assert append_started.wait(timeout=5)
    assert not append_finished.wait(timeout=0.2)

    release_check.set()
    settlement.join(timeout=5)
    appender.join(timeout=5)
    assert not settlement.is_alive()
    assert not appender.is_alive()
    assert errors == []
    assert apply_results == [(True, "acknowledged")]
    assert not (clean_queue._queue_dir() / f"{task_id}.processing").exists()

    monkeypatch.setattr(clean_queue, "_has_pending_compile_work", real_check)
    ensured = clean_queue.ensure_compile_task()
    assert ensured["created"] is True
    assert ensured["state"] == "pending_eligible"


def test_ensure_compile_task_reports_backoff_deadline(clean_queue):
    task_id = clean_queue.enqueue("compile", {"force": True})
    path = clean_queue._queue_dir() / f"{task_id}.json"
    task = json.loads(path.read_text(encoding="utf-8"))
    task["attempts"] = 1
    task["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
    clean_queue._atomic_write_json(path, task)

    ensured = clean_queue.ensure_compile_task()

    assert ensured["pending"] is True
    assert ensured["created"] is False
    assert ensured["task_id"] == task_id
    assert ensured["state"] == "backoff"
    assert 1 <= ensured["retry_delay_seconds"] <= clean_queue.RETRY_BACKOFF_SECONDS
    assert datetime.fromisoformat(ensured["eligible_at"]) > datetime.now()
    assert len(clean_queue.list_pending()) == 1


def test_ensure_compile_task_reports_terminal_control(clean_queue):
    task_id = clean_queue.enqueue("compile", {"force": True})
    path = clean_queue._queue_dir() / f"{task_id}.json"
    task = json.loads(path.read_text(encoding="utf-8"))
    task["attempts"] = clean_queue.MAX_ATTEMPTS
    clean_queue._atomic_write_json(path, task)

    ensured = clean_queue.ensure_compile_task()

    assert ensured == {
        "pending": True,
        "created": False,
        "task_id": task_id,
        "state": "terminal",
    }
    assert len(clean_queue.list_pending()) == 1


def test_renewed_sdk_lease_resists_recovery_until_renewal_is_stale(
    clean_queue, monkeypatch, capsys
):
    clean_queue.enqueue("query", {"prompt": "long provider call"})
    prepared = clean_queue.prepare_sdk_task()
    lease_path = clean_queue._queue_dir() / f"{prepared['task_id']}.processing"

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "--renew-sdk-task",
        stdin=json.dumps(prepared),
    )

    assert code == 0, captured.err
    assert json.loads(captured.out) == {"ok": True, "status": "renewed"}
    renewed = json.loads(lease_path.read_text(encoding="utf-8"))
    assert renewed["_sdk_lease"]["renewed_at"]
    assert clean_queue._task_digest(renewed) == prepared["digest"]

    old_timestamp = datetime(2000, 1, 1).timestamp()
    os.utime(lease_path, (old_timestamp, old_timestamp))
    assert clean_queue.recover_stale_leases(max_age_seconds=60) == 0

    stale = json.loads(lease_path.read_text(encoding="utf-8"))
    stale["_sdk_lease"]["renewed_at"] = "2000-01-01T00:00:00"
    stale["_sdk_lease"]["leased_at"] = "2000-01-01T00:00:00"
    lease_path.write_text(json.dumps(stale), encoding="utf-8")
    os.utime(lease_path, (old_timestamp, old_timestamp))
    assert clean_queue.recover_stale_leases(max_age_seconds=60) == 1

    [pending] = clean_queue.list_pending()
    assert pending["attempts"] == 0
    assert not list(clean_queue._queue_dir().glob("*.processing"))


def test_renew_sdk_lease_rejects_wrong_identity_without_mutation(clean_queue):
    clean_queue.enqueue("query", {"prompt": "identity"})
    prepared = clean_queue.prepare_sdk_task()
    lease_path = clean_queue._queue_dir() / f"{prepared['task_id']}.processing"
    original = lease_path.read_bytes()

    assert clean_queue.renew_sdk_task(
        {**prepared, "lease_id": "wrong-lease"}
    ) == (False, "stale SDK task identity or digest")
    assert clean_queue.renew_sdk_task(
        {**prepared, "digest": "0" * 64}
    ) == (False, "stale SDK task identity or digest")
    assert lease_path.read_bytes() == original


def test_status_counts_processing_lease_as_outstanding(clean_queue):
    clean_queue.enqueue("query", {"prompt": "in flight"})
    prepared = clean_queue.prepare_sdk_task()

    current = clean_queue.status()

    assert prepared["pending"] is True
    assert current["pending_total"] == 0
    assert current["in_flight"] == 1
    assert current["in_flight_by_type"] == {"query": 1}
    assert current["outstanding_total"] == 1
    assert current["permanently_failed"] == 0


@pytest.mark.parametrize("max_tokens", ["4000", True, 0, -1, 100_001, []])
def test_prepare_terminally_releases_invalid_max_tokens(clean_queue, max_tokens):
    task_id = clean_queue.enqueue(
        "query", {"prompt": "validate tokens", "max_tokens": max_tokens}
    )

    assert clean_queue.prepare_sdk_task() == {"pending": False}

    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["attempts"] == clean_queue.MAX_ATTEMPTS
    assert pending["terminal_failure"] is True
    assert "invalid max_tokens" in pending["last_error"]
    assert not list(clean_queue._queue_dir().glob("*.processing"))


@pytest.mark.parametrize(
    ("task_type", "payload", "malformed", "reason"),
    [
        (
            "query",
            {"prompt": "validate result"},
            {"success": "true", "response": "answer"},
            "success must be a bool",
        ),
        (
            "query",
            {"prompt": "validate result"},
            {"response": "answer"},
            "success must be a bool",
        ),
        (
            "query",
            {"prompt": "validate result"},
            {"success": True, "response": {"text": "answer"}},
            "response must be a string",
        ),
        (
            "compile",
            {"force": True},
            {"defer": "true"},
            "defer must be a bool",
        ),
        (
            "compile",
            {"force": True},
            {"defer": True, "success": "true"},
            "success must be a bool",
        ),
        (
            "query",
            {"prompt": "validate result"},
            {"defer": True},
            "only compile controls may be deferred",
        ),
    ],
)
def test_malformed_sdk_result_records_failure_without_acknowledging(
    clean_queue, task_type, payload, malformed, reason
):
    task_id = clean_queue.enqueue(task_type, payload)
    prepared = clean_queue.prepare_sdk_task()

    assert clean_queue.apply_sdk_result({**prepared, **malformed}) == (
        True,
        "failure recorded",
    )

    [pending] = clean_queue.list_pending()
    assert pending["id"] == task_id
    assert pending["attempts"] == 1
    assert reason in pending["last_error"]
    assert not list(clean_queue._queue_dir().glob("*.processing"))
    assert not (clean_queue._queue_dir().parent / "queue-results").exists()


def _approve_queue_retry_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["approved"] = True
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_retry_failed_manifest_cycle_resets_only_retry_metadata(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "preserve payload"})
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["last_error"] = "provider unavailable"
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    exhausted = json.loads(task_path.read_text(encoding="utf-8"))

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "audit",
        "--task-id",
        task_id,
    )
    assert code == 0, captured.err
    assert json.loads(captured.out)["status"] == "eligible"

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    assert code == 0, captured.err
    prepared = json.loads(captured.out)
    manifest_path = Path(prepared["manifest"])
    assert prepared["status"] == "prepared"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["approved"] is False
    assert json.loads(task_path.read_text(encoding="utf-8")) == exhausted

    _approve_queue_retry_manifest(manifest_path)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )
    assert code == 0, captured.err
    assert json.loads(captured.out)["status"] == "applied"
    retried = json.loads(task_path.read_text(encoding="utf-8"))
    assert retried["attempts"] == 0
    assert retried["last_attempt_at"] is None
    assert retried["last_error"] == exhausted["last_error"]
    assert retried["payload"] == exhausted["payload"]
    assert retried["id"] == exhausted["id"]
    assert retried["enqueue_sequence"] == exhausted["enqueue_sequence"]

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "verify",
        "--manifest",
        str(manifest_path),
    )
    assert code == 0, captured.err
    assert json.loads(captured.out)["status"] == "verified"


def test_retry_failed_apply_rejects_task_drift_without_overwrite(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "original"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    assert code == 0, captured.err
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)

    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    drifted = json.loads(task_path.read_text(encoding="utf-8"))
    drifted["payload"]["prompt"] = "changed after review"
    task_path.write_text(json.dumps(drifted, indent=2) + "\n", encoding="utf-8")
    drifted_bytes = task_path.read_bytes()

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )
    assert code == 3
    assert "drift" in captured.err.lower()
    assert task_path.read_bytes() == drifted_bytes


def test_retry_failed_manifest_pins_reviewed_task_identity(clean_queue, monkeypatch, capsys):
    task_id = clean_queue.enqueue("query", {"prompt": "review exact identity"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )

    assert code == 0, captured.err
    manifest_path = Path(json.loads(captured.out)["manifest"])
    [entry] = json.loads(manifest_path.read_text(encoding="utf-8"))["tasks"]
    task = json.loads(
        (clean_queue._queue_dir() / f"{task_id}.json").read_text(encoding="utf-8")
    )
    assert entry["task_id"] == task_id
    assert entry["task_type"] == task["type"]
    assert entry["enqueue_sequence"] == task["enqueue_sequence"]
    assert entry["attempts"] == clean_queue.MAX_ATTEMPTS
    assert entry["last_error"] == task.get("last_error")
    assert entry["payload_identity_sha256"] == hashlib.sha256(
        json.dumps(
            task["payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def test_retry_failed_verify_rejects_missing_task(clean_queue, monkeypatch, capsys):
    task_id = clean_queue.enqueue("query", {"prompt": "must remain pending"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )
    assert code == 0, captured.err
    (clean_queue._queue_dir() / f"{task_id}.json").unlink()

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "verify",
        "--manifest",
        str(manifest_path),
    )

    assert code == 3
    assert "missing" in captured.err.lower()


def test_retry_failed_partial_apply_blocks_claim_and_resumes(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_ids = [
        clean_queue.enqueue("query", {"prompt": f"task {index}"})
        for index in range(2)
    ]
    for task_id in task_ids:
        for _ in range(clean_queue.MAX_ATTEMPTS):
            clean_queue.mark_attempt(task_id, success=False)
    arguments = [
        item
        for task_id in task_ids
        for item in ("--task-id", task_id)
    ]
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        *arguments,
    )
    assert code == 0, captured.err
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)

    real_write = clean_queue._atomic_write_json
    writes = 0

    def interrupted(path, value):
        nonlocal writes
        if path.suffix == ".json" and path.parent == clean_queue._queue_dir():
            writes += 1
            if writes == 2:
                raise OSError("injected retry interruption")
        return real_write(path, value)

    monkeypatch.setattr(clean_queue, "_atomic_write_json", interrupted)
    with pytest.raises(OSError, match="retry interruption"):
        clean_queue.retry_failed_apply(manifest_path)
    assert clean_queue._queue_retry_barrier_path().is_file()
    with pytest.raises(clean_queue.QueueIntegrityError, match="retry transaction"):
        clean_queue._claim_next_task()

    monkeypatch.setattr(clean_queue, "_atomic_write_json", real_write)
    assert clean_queue.retry_failed_apply(manifest_path)["status"] == "applied"
    assert not clean_queue._queue_retry_barrier_path().exists()
    assert [task["attempts"] for task in clean_queue.list_pending()] == [0, 0]


def test_retry_failed_apply_requires_approval_without_touching_task(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "approval required"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    before = task_path.read_bytes()
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )

    assert code == 3
    assert "approved" in captured.err.lower()
    assert task_path.read_bytes() == before


def test_retry_failed_apply_rejects_tampered_artifact(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "sealed artifact"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_path = manifest_path.parent / manifest["tasks"][0]["after_artifact"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["payload"]["prompt"] = "tampered"
    artifact_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )

    assert code == 3
    assert "digest" in captured.err.lower()


def test_retry_failed_apply_rejects_task_that_became_leased(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "lease drift"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    task_path.rename(task_path.with_suffix(".processing"))

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )

    assert code == 3
    assert "leased" in captured.err.lower()


def test_retry_failed_rejects_legacy_task_without_durable_sequence(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = "20200101-000000-00000001"
    (clean_queue._queue_dir() / f"{task_id}.json").write_text(
        json.dumps(
            {
                "id": task_id,
                "type": "query",
                "enqueued_at": "2020-01-01T00:00:00",
                "attempts": clean_queue.MAX_ATTEMPTS,
                "last_attempt_at": "2020-01-01T00:01:00",
                "payload": {"prompt": "legacy"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "audit",
        "--task-id",
        task_id,
    )

    assert code == 3
    assert "sequence" in json.loads(captured.out)["diagnostic"].lower()
    assert not (clean_queue._queue_dir().parent / "backups").exists()


def test_retry_failed_reapply_is_idempotent_and_next_lease_uses_reviewed_digest(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "lease reviewed postimage"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    assert clean_queue.retry_failed_apply(manifest_path)["status"] == "applied"

    assert clean_queue.retry_failed_apply(manifest_path)["status"] == "already_applied"
    [entry] = json.loads(manifest_path.read_text(encoding="utf-8"))["tasks"]
    prepared = clean_queue.prepare_sdk_task()
    assert prepared["task_id"] == task_id
    assert prepared["digest"] == entry["after_digest"]


def test_retry_failed_barrier_publication_failure_precedes_task_mutation(
    clean_queue,
    monkeypatch,
    capsys,
):
    import memory_state

    task_id = clean_queue.enqueue("query", {"prompt": "barrier first"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    before = task_path.read_bytes()
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    real_atomic_write = memory_state.atomic_write

    def interrupted(path, content, encoding="utf-8"):
        if Path(path).name == clean_queue.QUEUE_RETRY_BARRIER_NAME:
            raise OSError("injected barrier publication failure")
        return real_atomic_write(path, content, encoding)

    monkeypatch.setattr(memory_state, "atomic_write", interrupted)

    with pytest.raises(OSError, match="barrier publication"):
        clean_queue.retry_failed_apply(manifest_path)
    assert task_path.read_bytes() == before
    assert not clean_queue._queue_retry_barrier_path().exists()


def test_retry_failed_apply_enforces_aggregate_artifact_bound(
    clean_queue,
    monkeypatch,
    capsys,
):
    task_id = clean_queue.enqueue("query", {"prompt": "bounded package"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    before = task_path.read_bytes()
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    monkeypatch.setattr(clean_queue, "MAX_QUEUE_RETRY_BACKUP_BYTES", 1)

    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "apply",
        "--manifest",
        str(manifest_path),
    )

    assert code == 3
    assert "aggregate" in captured.err.lower()
    assert task_path.read_bytes() == before
    assert not clean_queue._queue_retry_barrier_path().exists()


def test_retry_failed_barrier_durability_failure_blocks_before_task_mutation(
    clean_queue,
    monkeypatch,
    capsys,
):
    import memory_state

    task_id = clean_queue.enqueue("query", {"prompt": "durable barrier"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    task_path = clean_queue._queue_dir() / f"{task_id}.json"
    before = task_path.read_bytes()
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    real_sync = memory_state.sync_parent_directory_strict

    def failed_sync(path):
        if Path(path).name == clean_queue.QUEUE_RETRY_BARRIER_NAME:
            raise OSError("injected barrier durability failure")
        return real_sync(path)

    monkeypatch.setattr(memory_state, "sync_parent_directory_strict", failed_sync)

    with pytest.raises(OSError, match="barrier durability"):
        clean_queue.retry_failed_apply(manifest_path)
    assert task_path.read_bytes() == before
    assert clean_queue._queue_retry_barrier_path().is_file()
    with pytest.raises(clean_queue.QueueIntegrityError, match="retry transaction"):
        clean_queue._claim_next_task()

    monkeypatch.setattr(memory_state, "sync_parent_directory_strict", real_sync)
    assert clean_queue.retry_failed_apply(manifest_path)["status"] == "applied"
    assert not clean_queue._queue_retry_barrier_path().exists()


def test_retry_failed_postimage_durability_failure_keeps_barrier(
    clean_queue,
    monkeypatch,
    capsys,
):
    import memory_state

    task_id = clean_queue.enqueue("query", {"prompt": "durable postimage"})
    for _ in range(clean_queue.MAX_ATTEMPTS):
        clean_queue.mark_attempt(task_id, success=False)
    code, captured = _run_cli(
        clean_queue,
        monkeypatch,
        capsys,
        "retry-failed",
        "--phase",
        "backup-only",
        "--task-id",
        task_id,
    )
    manifest_path = Path(json.loads(captured.out)["manifest"])
    _approve_queue_retry_manifest(manifest_path)
    real_sync = memory_state.sync_file_strict

    def failed_sync(path):
        if Path(path).parent == clean_queue._queue_dir():
            raise OSError("injected postimage durability failure")
        return real_sync(path)

    monkeypatch.setattr(memory_state, "sync_file_strict", failed_sync)

    with pytest.raises(OSError, match="postimage durability"):
        clean_queue.retry_failed_apply(manifest_path)
    assert clean_queue._queue_retry_barrier_path().is_file()
    with pytest.raises(clean_queue.QueueIntegrityError, match="retry transaction"):
        clean_queue._claim_next_task()

    synced = []

    def tracked_sync(path):
        synced.append(Path(path))
        return real_sync(path)

    monkeypatch.setattr(memory_state, "sync_file_strict", tracked_sync)
    assert clean_queue.retry_failed_apply(manifest_path)["status"] == "applied"
    assert clean_queue._queue_dir() / f"{task_id}.json" in synced
    assert not clean_queue._queue_retry_barrier_path().exists()
