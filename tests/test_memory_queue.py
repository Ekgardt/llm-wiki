"""Behavior tests for the fenced SQLite deferred-work queue."""

from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_queue import (  # noqa: E402
    DeferredResult,
    LeaseFenceError,
    MemoryQueue,
    QueueFailure,
    QueueOperationError,
    ResultConflictError,
)
from reliable_memory import canonical_json_bytes, sha256_bytes  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ReverseIdRng:
    def __init__(self) -> None:
        self.values = iter((2, 1, 3, 4))

    def getrandbits(self, bits: int) -> int:
        del bits
        return next(self.values)

    def uniform(self, low: float, high: float) -> float:
        del low
        return high


class CustomSequence:
    def __init__(self, *items: object) -> None:
        self.items = items

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> object:
        return self.items[index]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(tmp_path: Path, clock: FakeClock) -> MemoryQueue:
    return MemoryQueue(tmp_path, clock=clock, rng=random.Random(7))


def test_enqueue_stores_closed_canonical_redacted_payload(queue: MemoryQueue) -> None:
    task_id = queue.enqueue(
        "query",
        2,
        {"z": "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345", "a": ["ok"]},
    )

    task = queue.get(task_id)
    assert task.kind == "query"
    assert task.handler_version == 2
    assert task.state == "ready"
    assert task.priority == 0
    assert task.payload == {"a": ["ok"], "z": "Authorization: Bearer [REDACTED]"}
    assert task.input_hash == sha256_bytes(canonical_json_bytes(task.payload))
    with sqlite3.connect(queue.db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        stored = connection.execute(
            "SELECT payload_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
    assert "payload" not in columns
    assert json.loads(stored) == task.payload


@pytest.mark.parametrize("state", ["ready", "leased", "blocked", "dead"])
def test_source_fence_rejects_every_referencing_non_success_state(
    queue: MemoryQueue, state: str
) -> None:
    daily_id = "2026-01-01"
    digest = "a" * 64
    task_id = queue.enqueue(
        "compile", 1, {"daily_id": daily_id, "source_digest": digest}
    )
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))

    with pytest.raises(QueueOperationError, match="source_referenced"):
        queue.acquire_source_fence(daily_id, digest)


def test_source_fence_atomically_blocks_enqueue_claim_and_redrive(
    queue: MemoryQueue,
) -> None:
    daily_id = "2026-01-01"
    digest = "b" * 64
    fence = queue.acquire_source_fence(daily_id, digest)

    with pytest.raises(QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"daily_id": daily_id})
    with pytest.raises(QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"source_digest": digest})

    # A legacy/external writer cannot make fenced work claimable.
    payload = canonical_json_bytes({"daily_id": daily_id}).decode()
    now = "2026-07-14T12:00:00.000000+00:00"
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, state,
                   priority, created_at, updated_at, available_at
               ) VALUES ('injected', 'compile', 1, ?, ?, 'ready', 0, ?, ?, ?)""",
            (payload, sha256_bytes(payload.encode()), now, now, now),
        )
    assert queue.claim("worker") is None

    with sqlite3.connect(queue.db_path) as connection:
        connection.execute("UPDATE tasks SET state='dead' WHERE id='injected'")
    with pytest.raises(QueueOperationError, match="source_fenced"):
        queue.redrive("injected")

    queue.release_source_fence(fence.token)
    assert queue.enqueue("compile", 1, {"daily_id": daily_id})


def test_source_fence_requires_canonical_daily_and_digest(queue: MemoryQueue) -> None:
    with pytest.raises(ValueError):
        queue.acquire_source_fence("../2026-01-01", "a" * 64)
    with pytest.raises(ValueError):
        queue.acquire_source_fence("2026-01-01", "A" * 64)


def test_source_finalization_rejects_expired_fence(queue: MemoryQueue) -> None:
    daily_id = "2026-01-01"
    digest = "9" * 64
    fence = queue.acquire_source_fence(daily_id, digest)
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE source_fences SET expires_at=? WHERE token=?",
            ("2000-01-01T00:00:00+00:00", fence.token),
        )

    with pytest.raises(QueueOperationError, match="source_fence_lost"):
        with queue.source_finalization(fence):
            pytest.fail("expired fence must not finalize")


def test_source_finalization_rejects_task_injected_after_fence(
    queue: MemoryQueue,
) -> None:
    daily_id = "2026-01-01"
    digest = "8" * 64
    fence = queue.acquire_source_fence(daily_id, digest)
    payload = canonical_json_bytes(
        {"daily_id": daily_id, "source_digest": digest}
    ).decode()
    now = "2026-07-14T12:00:00.000000+00:00"
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, state,
                   priority, created_at, updated_at, available_at
               ) VALUES ('injected-finalization', 'compile', 1, ?, ?, 'ready',
                         0, ?, ?, ?)""",
            (payload, sha256_bytes(payload.encode()), now, now, now),
        )

    with pytest.raises(QueueOperationError, match="source_referenced"):
        with queue.source_finalization(fence):
            pytest.fail("referenced source must not finalize")


def test_queue_failure_persists_and_success_clears_source_failure(
    queue: MemoryQueue,
) -> None:
    logical_path = "knowledge/daily/2026-01-01.md"
    digest = "c" * 64
    failed_id = queue.enqueue(
        "compile", 1, {"source_path": logical_path, "source_digest": digest}
    )
    failed = queue.claim("worker")
    assert failed is not None and failed.id == failed_id
    queue.fail(failed, QueueFailure("provider_failed", permanent=True))

    record = queue.source_failure(logical_path, digest)
    assert record == {
        "logical_path": logical_path,
        "source_digest": digest,
        "error_code": "provider_failed",
        "producer": "queue",
    }

    success_id = queue.enqueue(
        "compile", 1, {"source_path": logical_path, "source_digest": digest}
    )
    success = queue.claim("worker")
    assert success is not None and success.id == success_id
    queue.publish_result(success, operation_id="compile-success", result=b"ok")
    queue.acknowledge(success)
    assert queue.source_failure(logical_path, digest) is None


def test_cancelled_terminal_resolution_clears_source_failure(queue: MemoryQueue) -> None:
    logical_path = "knowledge/daily/2026-01-01.md"
    digest = "d" * 64
    queue.record_source_failure(
        logical_path,
        digest,
        error_code="blocked",
        producer="queue",
    )
    task_id = queue.enqueue(
        "compile", 1, {"source_path": logical_path, "source_digest": digest}
    )

    assert queue.cancel(task_id)
    assert queue.source_failure(logical_path, digest) is None


def test_source_failure_retains_run_directory(queue: MemoryQueue) -> None:
    logical_path = "knowledge/daily/2026-01-01.md"
    digest = "f" * 64
    queue.record_source_failure(
        logical_path,
        digest,
        error_code="compile_failed",
        producer="compile",
    )

    assert queue.retains_run_directory() is True
    queue.clear_source_failure(logical_path, digest)
    assert queue.retains_run_directory() is False


def test_enqueue_recursively_redacts_secret_keys_and_value_patterns(
    queue: MemoryQueue,
) -> None:
    task_id = queue.enqueue(
        "query",
        1,
        {
            "password": "plain",
            "nested": {
                "API-Key": "plain-api-key",
                "items": [
                    {"authorization": "Basic abc"},
                    {"safe": "Bearer token: ghp_abcdefghijklmnopqrstuvwxyz012345"},
                    {"cookie": {"session": "secret"}},
                ],
            },
            "safe": "ok",
        },
    )
    payload = queue.get(task_id).payload
    assert payload == {
        "nested": {
            "API-Key": "[REDACTED]",
            "items": [
                {"authorization": "[REDACTED]"},
                {"safe": "Bearer token: [REDACTED]"},
                {"cookie": "[REDACTED]"},
            ],
        },
        "password": "[REDACTED]",
        "safe": "ok",
    }


def test_enqueue_converts_nested_tuples_to_redacted_json_arrays(
    queue: MemoryQueue,
) -> None:
    task_id = queue.enqueue(
        "query",
        1,
        {
            "items": (
                {"password": "plain"},
                ("ok", {"api_key": "plain", "safe": "token=secret-value"}),
            )
        },
    )
    assert queue.get(task_id).payload == {
        "items": [
            {"password": "[REDACTED]"},
            ["ok", {"api_key": "[REDACTED]", "safe": "token=[REDACTED]"}],
        ]
    }


@pytest.mark.parametrize("unsupported", [{"value"}, CustomSequence("value")])
def test_enqueue_rejects_unsupported_collections(
    queue: MemoryQueue, unsupported: object
) -> None:
    with pytest.raises(TypeError, match="canonical JSON does not permit"):
        queue.enqueue("query", 1, {"items": unsupported})


def test_enqueue_validates_priority_and_deduplicates(queue: MemoryQueue) -> None:
    first = queue.enqueue("compile", 1, {"day": "2026-07-14"}, dedupe_key="day:14")
    second = queue.enqueue("compile", 1, {"day": "changed"}, dedupe_key="day:14")
    assert second == first
    with pytest.raises(ValueError, match="priority"):
        queue.enqueue("query", 1, {}, priority=101)
    with pytest.raises(ValueError, match="handler_version"):
        queue.enqueue("query", 0, {})


def test_claim_orders_priority_then_availability_then_fifo(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    low = queue.enqueue("query", 1, {"prompt": "low"}, priority=-1)
    first = queue.enqueue("query", 1, {"prompt": "first"}, priority=10)
    clock.advance(1)
    second = queue.enqueue("query", 1, {"prompt": "second"}, priority=10)

    assert [queue.claim("w").id, queue.claim("w").id, queue.claim("w").id] == [
        first,
        second,
        low,
    ]


def test_claim_is_fifo_when_creation_timestamps_are_equal(
    tmp_path: Path, clock: FakeClock
) -> None:
    queue = MemoryQueue(tmp_path, clock=clock, rng=ReverseIdRng())
    first = queue.enqueue("query", 1, {"prompt": "first"})
    second = queue.enqueue("query", 1, {"prompt": "second"})
    assert first > second  # Prove ID sorting would produce the wrong order.
    assert queue.claim("worker").id == first
    assert queue.claim("worker").id == second


def test_claim_honors_availability_and_uses_random_tokens(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    future = queue.enqueue(
        "query", 1, {"prompt": "later"}, available_at=clock() + timedelta(seconds=5)
    )
    assert queue.claim("worker") is None
    clock.advance(5)
    first = queue.claim("worker")
    assert first is not None and first.id == future
    queue.fail(first, QueueFailure("retry"))
    clock.advance(30)
    second = queue.claim("worker")
    assert second is not None
    assert second.token != first.token


def test_claim_has_exact_public_signature() -> None:
    parameters = signature(MemoryQueue.claim).parameters
    assert list(parameters) == ["self", "owner", "lease_seconds", "max_attempts"]
    assert parameters["lease_seconds"].kind.name == "KEYWORD_ONLY"
    assert parameters["lease_seconds"].default == 120


def test_heartbeat_result_and_acknowledge_are_fenced(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    task_id = queue.enqueue("query", 1, {"prompt": "x"})
    old = queue.claim("old", lease_seconds=120)
    assert old is not None
    renewed = queue.heartbeat(old, lease_seconds=180)
    assert renewed.expires_at == clock() + timedelta(seconds=180)
    clock.advance(181)
    current = queue.claim("new", lease_seconds=120)
    assert current is not None

    for action in (
        lambda: queue.heartbeat(old),
        lambda: queue.publish_result(old, operation_id=task_id, result=b"old"),
        lambda: queue.acknowledge(old),
    ):
        with pytest.raises(LeaseFenceError):
            action()

    reference = queue.publish_result(current, operation_id=task_id, result=b"new")
    terminal = queue.acknowledge(current)
    assert terminal.state == "succeeded"
    assert terminal.error_code is None
    assert queue.get(task_id).state == "succeeded"
    assert (queue.state_root / reference).read_bytes() == b"new"


def test_result_publication_is_stable_owner_only_and_no_clobber(
    queue: MemoryQueue,
) -> None:
    queue.enqueue("query", 1, {"prompt": "x"})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id="stable-op", result=b"answer")
    assert queue.publish_result(lease, operation_id="stable-op", result=b"answer") == reference
    with pytest.raises(ResultConflictError):
        queue.publish_result(lease, operation_id="stable-op", result=b"different")
    result_path = queue.state_root / reference
    assert queue.get(lease.id).result_sha256 == sha256_bytes(b"answer")
    if sys.platform != "win32":
        assert stat.S_IMODE(result_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("branch", ["precheck", "link_race"])
def test_publish_existing_same_digest_is_bounded_idempotent_without_read_bytes(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"same")
    result_path = queue.state_root / reference
    real_exists = Path.exists
    if branch == "link_race":
        monkeypatch.setattr(
            Path,
            "exists",
            lambda path: False if path == result_path else real_exists(path),
        )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: pytest.fail("publish_result must use bounded validator"),
    )

    assert queue.publish_result(lease, operation_id=task_id, result=b"same") == reference
    assert queue.get(task_id).state == "leased"


def test_publish_existing_mismatch_is_bounded_conflict_without_overwrite(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: pytest.fail("publish_result must use bounded validator"),
    )

    with pytest.raises(ResultConflictError, match="different result bytes"):
        queue.publish_result(lease, operation_id=task_id, result=b"different")
    with result_path.open("rb") as handle:
        assert handle.read() == b"original"
    assert queue.get(task_id).state == "leased"


@pytest.mark.parametrize("invalid", ["symlink", "oversize", "changed", "owner"])
def test_publish_existing_invalid_metadata_dead_letters_without_overwrite(
    queue: MemoryQueue,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    import memory_queue

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    if invalid == "symlink":
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == result_path or real_is_symlink(path),
        )
    elif invalid == "oversize":
        monkeypatch.setattr(memory_queue, "_MAX_RESULT_BYTES", 1)
    elif invalid == "changed":
        monkeypatch.setattr(queue, "_validated_result_digest", lambda reference: None)
    else:
        monkeypatch.setattr(memory_queue, "_is_owner_only", lambda path: False)

    incoming = b"x" if invalid == "oversize" else b"original"
    with pytest.raises(ResultConflictError, match="result_corrupt"):
        queue.publish_result(lease, operation_id=task_id, result=incoming)
    task = queue.get(task_id)
    assert task.state == "dead"
    assert task.error_code == "result_corrupt"
    with result_path.open("rb") as handle:
        assert handle.read() == b"original"


def test_expired_lease_with_published_result_reconciles_without_redelivery(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    task_id = queue.enqueue("query", 1, {"prompt": "nondeterministic"})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    reference = queue.publish_result(
        lease, operation_id=task_id, result=b"first-and-only-result"
    )
    assert queue.get(task_id).state == "leased"  # Crash before acknowledge.
    clock.advance(6)

    assert queue.claim("replacement") is None
    task = queue.get(task_id)
    assert task.state == "succeeded"
    assert task.result_reference == reference
    assert task.result_sha256 == sha256_bytes(b"first-and-only-result")
    assert len(task.attempt_history) == 1
    assert task.attempt_history[0].outcome == "succeeded"


def test_drain_does_not_rerun_handler_after_publish_before_ack_crash(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(12))
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    task_id = queue.enqueue("query", 1, {"prompt": "changes-every-time"})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"stable")
    clock.advance(6)
    calls: list[str] = []

    counts = memory_queue.drain_with(
        lambda task: calls.append(task["id"]) or DeferredResult(b"different"),
        max_tasks=1,
    )
    assert counts == {"ok": 0, "failed": 0, "dead": 0, "skipped": 0}
    assert calls == []
    assert queue.get(task_id).state == "succeeded"


@pytest.mark.parametrize("corruption", ["missing", "mismatch"])
def test_expired_published_result_corruption_goes_dead_without_overwrite(
    queue: MemoryQueue, clock: FakeClock, corruption: str
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    if corruption == "missing":
        result_path.unlink()
    else:
        result_path.write_bytes(b"tampered")
    clock.advance(6)

    assert queue.claim("replacement") is None
    task = queue.get(task_id)
    assert task.state == "dead"
    assert task.error_code == "result_corrupt"
    assert task.attempt_history[-1].error_code == "result_corrupt"
    if corruption == "mismatch":
        assert result_path.read_bytes() == b"tampered"


def test_drain_adopts_orphaned_valid_result_before_handler(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(13))
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"orphaned")
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """UPDATE tasks SET result_reference=NULL, result_operation_id=NULL,
                   result_sha256=NULL WHERE id=?""",
            (task_id,),
        )
    clock.advance(6)
    calls: list[str] = []

    counts = memory_queue.drain_with(
        lambda task: calls.append(task["id"]) or DeferredResult(b"new-output"),
        max_tasks=1,
    )
    assert counts == {"ok": 1, "failed": 0, "dead": 0, "skipped": 0}
    assert calls == []
    task = queue.get(task_id)
    assert task.state == "succeeded"
    assert task.result_reference == reference
    assert task.result_sha256 == sha256_bytes(b"orphaned")


@pytest.mark.parametrize("mutation", ["delete", "chmod", "corrupt"])
def test_acknowledge_rejects_result_mutated_after_publish(
    queue: MemoryQueue,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    import memory_queue

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    if mutation == "delete":
        result_path.unlink()
    elif mutation == "chmod":
        if os.name == "posix":
            result_path.chmod(0o644)
        else:
            monkeypatch.setattr(memory_queue, "_is_owner_only", lambda path: False)
    else:
        result_path.write_bytes(b"corrupt")

    terminal = queue.acknowledge(lease)
    assert terminal.state == "dead"
    assert terminal.error_code == "result_corrupt"
    task = queue.get(task_id)
    assert task.state == "dead"
    assert task.error_code == "result_corrupt"
    assert task.attempt_history[-1].outcome == "failed"
    assert task.attempt_history[-1].error_code == "result_corrupt"


def test_acknowledge_rejects_result_reference_outside_results_dir(
    queue: MemoryQueue,
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"original")
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET result_reference='run/outside.result' WHERE id=?",
            (task_id,),
        )

    queue.acknowledge(lease)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_acknowledge_rejects_symlink_result(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    reference = queue.publish_result(lease, operation_id=task_id, result=b"original")
    result_path = queue.state_root / reference
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == result_path or real_is_symlink(path),
    )

    queue.acknowledge(lease)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_acknowledge_rejects_result_over_size_bound(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"two bytes")
    monkeypatch.setattr(memory_queue, "_MAX_RESULT_BYTES", 1, raising=False)

    queue.acknowledge(lease)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_orphan_adoption_rejects_invalid_full_metadata_before_handler(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(14))
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("crashed", lease_seconds=5)
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"too-large")
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """UPDATE tasks SET result_reference=NULL, result_operation_id=NULL,
                   result_sha256=NULL WHERE id=?""",
            (task_id,),
        )
    monkeypatch.setattr(memory_queue, "_MAX_RESULT_BYTES", 1, raising=False)
    clock.advance(6)
    calls: list[str] = []

    counts = memory_queue.drain_with(
        lambda task: calls.append(task["id"]) or DeferredResult(b"new"), max_tasks=1
    )
    assert counts == {"ok": 0, "failed": 1, "dead": 1, "skipped": 0}
    assert calls == []
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).error_code == "result_corrupt"


def test_result_directory_is_fsynced_before_database_reference(
    queue: MemoryQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    monkeypatch.setattr(
        memory_queue,
        "fsync_directory",
        lambda path: (_ for _ in ()).throw(OSError("simulated power loss")),
        raising=False,
    )
    with pytest.raises(OSError, match="power loss"):
        queue.publish_result(lease, operation_id=task_id, result=b"answer")
    assert queue.get(task_id).result_reference is None


def test_queue_hardens_directory_database_temp_and_result(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    protected: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        memory_queue,
        "_harden_owner_only",
        lambda path, mode: protected.append((Path(path), mode)),
        raising=False,
    )
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=task_id, result=b"answer")
    paths = {path for path, _mode in protected}
    assert queue.run_dir in paths
    assert queue.results_dir in paths
    assert queue.db_path in paths
    assert any(path.suffix == ".tmp" for path in paths)
    assert queue.state_root / queue.get(task_id).result_reference in paths


def test_queue_acl_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    monkeypatch.setattr(
        memory_queue,
        "_harden_owner_only",
        lambda path, mode: (_ for _ in ()).throw(PermissionError("ACL denied")),
        raising=False,
    )
    with pytest.raises(PermissionError, match="ACL denied"):
        MemoryQueue(tmp_path)


def test_run_acl_is_hardened_before_first_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    hardened: list[Path] = []
    real_open = memory_queue.open_operational_db

    def record_hardening(path: Path, mode: int) -> None:
        del mode
        hardened.append(Path(path))

    def checked_open(path: Path, *, busy_ms: int) -> sqlite3.Connection:
        assert tmp_path.resolve() / "run" in hardened
        return real_open(path, busy_ms=busy_ms)

    monkeypatch.setattr(memory_queue, "_harden_owner_only", record_hardening)
    monkeypatch.setattr(memory_queue, "open_operational_db", checked_open)
    queue = MemoryQueue(tmp_path)
    assert queue.run_dir in hardened


def test_run_acl_failure_prevents_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    opened: list[Path] = []
    real_open = memory_queue.open_operational_db

    def deny_run(path: Path, mode: int) -> None:
        del mode
        if Path(path).name == "run":
            raise PermissionError("Windows owner-only ACL denied")

    monkeypatch.setattr(memory_queue, "_harden_owner_only", deny_run)

    def record_open(path: Path, *, busy_ms: int) -> sqlite3.Connection:
        opened.append(Path(path))
        return real_open(path, busy_ms=busy_ms)

    monkeypatch.setattr(memory_queue, "open_operational_db", record_open)
    with pytest.raises(PermissionError, match="owner-only ACL denied"):
        MemoryQueue(tmp_path)
    assert opened == []


def test_queue_uses_secure_rollback_journal_without_wal_files(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("query", 1, {})
    journal = Path(f"{queue.db_path}-journal")
    with queue._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE tasks SET priority=1 WHERE id=?", (task_id,))
        assert journal.is_file()
        assert not Path(f"{queue.db_path}-wal").exists()
        assert not Path(f"{queue.db_path}-shm").exists()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        if os.name == "posix":
            assert stat.S_IMODE(queue.run_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(queue.db_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(journal.stat().st_mode) & 0o077 == 0
        connection.rollback()


def test_acknowledge_without_published_result_goes_dead(queue: MemoryQueue) -> None:
    queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    terminal = queue.acknowledge(lease)
    assert terminal.state == "dead"
    assert terminal.error_code == "result_corrupt"
    assert queue.get(lease.id).state == "dead"
    assert queue.get(lease.id).error_code == "result_corrupt"


def test_dependency_block_does_not_consume_attempt(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("compile", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("provider_missing", blocked_capability="llm.compile"))
    task = queue.get(task_id)
    assert task.state == "blocked"
    assert task.attempts == 0
    assert task.blocked_capability == "llm.compile"
    assert task.attempt_history[-1].outcome == "blocked"


@pytest.mark.parametrize("code", ["invalid_input", "unsupported_version"])
def test_permanent_input_and_version_failures_go_dead(
    queue: MemoryQueue, code: str
) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure(code))
    assert queue.get(task_id).state == "dead"


def test_retry_uses_full_jitter_and_longer_retry_after(
    tmp_path: Path, clock: FakeClock
) -> None:
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("temporary", retry_after=90))
    task = queue.get(task_id)
    assert task.state == "ready"
    assert task.attempts == 1
    assert task.available_at == clock() + timedelta(seconds=90)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (math.inf, None),
        (-math.inf, None),
        (math.nan, None),
        (-1, None),
        ("9" * 100, 604800.0),
        (10**100, 604800.0),
        (datetime.max.replace(tzinfo=timezone.utc), 604800.0),
    ],
)
def test_retry_after_is_finite_nonnegative_and_safely_bounded(
    clock: FakeClock, value: object, expected: float | None
) -> None:
    assert MemoryQueue._retry_after_seconds(value, clock()) == expected


def test_retry_without_retry_after_uses_seeded_full_jitter(
    tmp_path: Path, clock: FakeClock
) -> None:
    rng = random.Random(1)
    expected = random.Random(1)
    expected.getrandbits(128)  # enqueue task ID
    expected.getrandbits(256)  # lease token
    queue = MemoryQueue(tmp_path, clock=clock, rng=rng)
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("temporary"))
    delay = (queue.get(task_id).available_at - clock()).total_seconds()
    assert delay == pytest.approx(expected.uniform(0, 30))
    assert 0 <= delay <= 30


def test_seeded_full_jitter_reaches_3600_second_cap(
    tmp_path: Path, clock: FakeClock
) -> None:
    queue = MemoryQueue(tmp_path, clock=clock, rng=ReverseIdRng())
    assert queue._retry_delay(8) == 3600


def test_eighth_failure_is_dead_and_history_is_immutable(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    task_id = queue.enqueue("query", 1, {})
    previous = ()
    for attempt in range(1, 9):
        lease = queue.claim("worker")
        assert lease is not None
        queue.fail(lease, QueueFailure("temporary", retry_after=3601))
        task = queue.get(task_id)
        assert task.attempt_history[: len(previous)] == previous
        previous = task.attempt_history
        if attempt < 8:
            clock.advance(3601)
    assert queue.get(task_id).state == "dead"
    assert queue.get(task_id).attempts == 8
    assert len(queue.get(task_id).attempt_history) == 8


def test_attempt_history_rejects_database_updates_and_deletes(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("temporary"))

    for statement in (
        "UPDATE attempt_history SET error_code='changed' WHERE task_id=?",
        "DELETE FROM attempt_history WHERE task_id=?",
    ):
        with sqlite3.connect(queue.db_path) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="attempt history is immutable"):
                connection.execute(statement, (task_id,))


def test_eighth_expired_lease_goes_dead_without_ninth_claim(
    queue: MemoryQueue, clock: FakeClock
) -> None:
    task_id = queue.enqueue("query", 1, {})
    for attempt in range(1, 9):
        lease = queue.claim("worker", lease_seconds=1)
        assert lease is not None and lease.attempt == attempt
        clock.advance(2)

    assert queue.claim("worker", lease_seconds=1) is None
    task = queue.get(task_id)
    assert task.state == "dead"
    assert task.attempts == 8
    assert task.error_code == "attempts_exhausted"
    assert len(task.attempt_history) == 8
    assert task.attempt_history[-1].outcome == "lease_expired"
    assert task.attempt_history[-1].error_code == "attempts_exhausted"


def test_startup_retires_legacy_ready_task_at_attempt_limit(
    tmp_path: Path, clock: FakeClock
) -> None:
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue("query", 1, {})
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET attempts=8, state='ready' WHERE id=?", (task_id,)
        )

    restarted = MemoryQueue(tmp_path, clock=clock, rng=random.Random(2))
    task = restarted.get(task_id)
    assert task.state == "dead"
    assert task.error_code == "attempts_exhausted"
    assert task.attempt_history == ()


def test_startup_retirement_persists_source_failure(
    tmp_path: Path, clock: FakeClock
) -> None:
    logical_path = "knowledge/daily/2026-01-01.md"
    digest = "e" * 64
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue(
        "compile", 1, {"source_path": logical_path, "source_digest": digest}
    )
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET attempts=8, state='ready' WHERE id=?", (task_id,)
        )

    restarted = MemoryQueue(tmp_path, clock=clock, rng=random.Random(2))

    assert restarted.source_failure(logical_path, digest) == {
        "logical_path": logical_path,
        "source_digest": digest,
        "error_code": "attempts_exhausted",
        "producer": "queue",
    }


def test_startup_repair_preserves_existing_attempt_history(
    tmp_path: Path, clock: FakeClock
) -> None:
    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(8))
    task_id = queue.enqueue("query", 1, {})
    for attempt in range(8):
        lease = queue.claim("worker")
        assert lease is not None
        queue.fail(lease, QueueFailure("temporary", retry_after=1))
        if attempt < 7:
            retry_at = queue.get(task_id).available_at
            clock.advance((retry_at - clock()).total_seconds())
    original = queue.get(task_id).attempt_history
    assert len(original) == 8
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            "UPDATE tasks SET state='ready', error_code=NULL WHERE id=?", (task_id,)
        )

    repaired = MemoryQueue(tmp_path, clock=clock, rng=random.Random(9)).get(task_id)
    assert repaired.state == "dead"
    assert repaired.error_code == "attempts_exhausted"
    assert repaired.attempt_history == original
    assert len(repaired.attempt_history) == 8


def test_cancel_only_changes_nonterminal_tasks(queue: MemoryQueue) -> None:
    ready = queue.enqueue("query", 1, {"n": 1})
    succeeded = queue.enqueue("query", 1, {"n": 2}, priority=1)
    lease = queue.claim("worker")
    assert lease is not None
    queue.publish_result(lease, operation_id=succeeded, result=b"ok")
    queue.acknowledge(lease)

    assert queue.cancel(ready) is True
    assert queue.get(ready).state == "cancelled"
    assert queue.cancel(succeeded) is False
    assert queue.get(succeeded).state == "succeeded"


def test_cancel_expired_deadline_does_not_mutate_task(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("query", 1, {"n": 1})

    with pytest.raises(TimeoutError, match="deadline"):
        queue.cancel(task_id, deadline=time.monotonic() - 1)

    assert queue.get(task_id).state == "ready"


def test_redrive_expired_deadline_does_not_create_replacement(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))

    with pytest.raises(TimeoutError, match="deadline"):
        queue.redrive(task_id, deadline=time.monotonic() - 1)

    assert [task.id for task in queue.list_tasks()] == [task_id]


def test_queue_mutations_commit_before_deadline(queue: MemoryQueue) -> None:
    cancelled_id = queue.enqueue("query", 1, {"action": "cancel"})
    dead_id = queue.enqueue("query", 1, {"action": "redrive"}, priority=1)
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input", permanent=True))
    deadline = time.monotonic() + 5

    assert queue.cancel(cancelled_id, deadline=deadline) is True
    replacement = queue.redrive(dead_id, deadline=deadline)

    assert queue.get(cancelled_id).state == "cancelled"
    assert queue.get(replacement).redrive_of == dead_id


def test_dead_tasks_are_retained(queue: MemoryQueue) -> None:
    task_id = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    queue.fail(lease, QueueFailure("invalid_input"))
    assert queue.get(task_id).state == "dead"
    assert queue.claim("worker") is None
    assert queue.get(task_id).id == task_id


def test_sqlite_uses_required_durability_settings(queue: MemoryQueue) -> None:
    with sqlite3.connect(queue.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_upgrades_legacy_eight_attempt_constraint(tmp_path: Path) -> None:
    queue = MemoryQueue(tmp_path)
    with sqlite3.connect(queue.db_path) as connection:
        task_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0]
        connection.executescript(
            """DROP TABLE attempt_history;
               DROP TABLE tasks;"""
        )
        connection.execute(
            task_sql.replace("attempts >= 0", "attempts BETWEEN 0 AND 8")
        )

    upgraded = MemoryQueue(tmp_path)
    task_id = upgraded.enqueue("query", 1, {})
    with sqlite3.connect(upgraded.db_path) as connection:
        connection.execute("UPDATE tasks SET attempts=9 WHERE id=?", (task_id,))
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0]

    assert "BETWEEN 0 AND 8" not in schema
    assert upgraded.get(task_id).attempts == 9


def test_module_facade_preserves_v1_shapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import memory_queue

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    task_id = memory_queue.enqueue("query", {"prompt": "hello"})
    pending = memory_queue.list_pending()
    assert pending[0]["id"] == task_id
    assert pending[0]["type"] == "query"
    assert pending[0]["handler_version"] == 1

    queue = memory_queue._queue()
    real_connect = queue._connect
    claim_connections: list[sqlite3.Connection] = []

    def tracked_connect() -> sqlite3.Connection:
        connection = real_connect()
        claim_connections.append(connection)
        return connection

    monkeypatch.setattr(queue, "_connect", tracked_connect)
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    in_transaction: list[bool] = []

    def processor(task: dict[str, object]) -> bool:
        in_transaction.append(claim_connections[-1].in_transaction)
        return True

    assert memory_queue.drain_with(processor, max_tasks=1) == {
        "ok": 1,
        "failed": 0,
        "dead": 0,
        "skipped": 0,
    }
    assert in_transaction == [False]
    assert memory_queue.list_pending() == []
    snapshot = memory_queue.status()
    assert snapshot["pending_total"] == 0
    assert snapshot["queue_dir"].endswith("run")


def test_drain_counts_corrupt_acknowledgement_as_failed_and_dead(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(15))
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    task_id = queue.enqueue("query", 1, {})
    real_acknowledge = queue.acknowledge

    def corrupt_then_acknowledge(lease):
        task = queue.get(lease.id)
        (queue.state_root / task.result_reference).write_bytes(b"corrupt")
        return real_acknowledge(lease)

    monkeypatch.setattr(queue, "acknowledge", corrupt_then_acknowledge)
    counts = memory_queue.drain_with(lambda task: True, max_tasks=1)
    assert counts == {"ok": 0, "failed": 1, "dead": 1, "skipped": 0}
    assert queue.get(task_id).state == "dead"


def test_cli_work_returns_nonzero_for_failed_or_dead_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memory_queue

    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "work"])
    monkeypatch.setattr(
        memory_queue,
        "run_worker",
        lambda processor, **kwargs: memory_queue.WorkerSummary(1, 0, 1, 1, 0),
    )
    assert memory_queue._cli() == 1


def test_compat_processor_receives_preclaim_metadata(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(2))
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    task_id = queue.enqueue("query", 1, {"prompt": "hello"})
    enqueued_at = queue.get(task_id).created_at.isoformat(timespec="seconds")
    first = queue.claim("first")
    assert first is not None
    clock.advance(7)
    failure_at = clock().isoformat(timespec="seconds")
    queue.fail(first, QueueFailure("temporary", retry_after=0))
    retry_at = queue.get(task_id).available_at
    clock.advance((retry_at - clock()).total_seconds())

    seen: list[dict[str, object]] = []
    assert memory_queue.drain_with(lambda task: seen.append(task) or True, max_tasks=1)[
        "ok"
    ] == 1
    assert seen[0]["enqueued_at"] == enqueued_at
    assert seen[0]["last_attempt_at"] == failure_at
    assert seen[0]["attempts"] == 1


def test_manual_flush_handler_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import daily_log_append
    import flush_memory
    import llm_client
    import memory_queue

    captured: list[tuple[Path, str, str]] = []
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: "major body")
    monkeypatch.setattr(flush_memory, "_classify_response", lambda result: ("major", result))
    monkeypatch.setattr(
        daily_log_append,
        "locked_append_once",
        lambda path, block, operation_id: captured.append(
            (Path(path), block, operation_id)
        )
        or True,
        raising=False,
    )

    assert memory_queue._manual_processor(
        {
            "id": "flush-id",
            "type": "flush",
            "payload": {
                "prompt": "summarize",
                "system_prompt": "system",
                "day": "2026-07-14",
                "event": "session-end",
                "session_id": "s1",
            },
        }
    )
    assert captured[0][0] == tmp_path / "knowledge" / "daily" / "2026-07-14.md"
    assert "major body" in captured[0][1]
    assert captured[0][2] == "flush-id"


def test_deferred_flush_operation_is_appended_once_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daily_log_append

    daily = tmp_path / "knowledge" / "daily" / "2026-07-14.md"
    (tmp_path / "knowledge" / "notes").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(daily_log_append, "_daily_lock", nullcontext)
    assert daily_log_append.locked_append_once(daily, "\nbody\n", "stable-op") is True
    assert daily_log_append.locked_append_once(daily, "\nbody\n", "stable-op") is False
    content = daily.read_text(encoding="utf-8")
    assert content.count("body") == 1
    assert "stable-op" not in content
    assert content.count("llm-wiki-operation:") == 1


@pytest.mark.parametrize("day", ["../outside", "2026-7-14", "2026-07-14/../../x", ""])
def test_deferred_flush_rejects_invalid_or_traversing_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: str
) -> None:
    import llm_client
    import memory_queue

    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: "must not run")
    assert memory_queue._manual_processor(
        {"id": "op", "type": "flush", "payload": {"prompt": "p", "day": day}}
    ) is False
    assert not list(tmp_path.rglob("*.md"))


def test_manual_query_returns_fenced_result_without_write_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_client
    import memory_queue

    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: "answer")
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda *args, **kwargs: pytest.fail("query results must use publish_result"),
    )
    result = memory_queue._manual_processor(
        {
            "id": "stable-query",
            "type": "query",
            "payload": {"prompt": "hello", "output_path": str(tmp_path / "escape.txt")},
        }
    )
    assert result.data == b"answer"


def test_manual_query_result_is_published_by_queue_fence(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_client
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(4))
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: "answer")
    task_id = queue.enqueue("query", 1, {"prompt": "hello"})
    assert memory_queue.drain_with(memory_queue._manual_processor, max_tasks=1)["ok"] == 1
    task = queue.get(task_id)
    assert task.state == "succeeded"
    assert (queue.state_root / task.result_reference).read_bytes() == b"answer"


def test_deferred_compile_runs_synchronously_and_uses_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import maybe_compile
    import memory_queue

    script = tmp_path / "scripts" / "compile_memory.py"
    script.parent.mkdir()
    script.write_text("raise SystemExit(0)\n", encoding="ascii")
    calls: list[tuple[list[str], Path]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs["cwd"]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr(memory_queue.subprocess, "run", run)
    monkeypatch.setattr(
        maybe_compile,
        "spawn_compile_if_idle",
        lambda **kwargs: pytest.fail("queue compile must not detach"),
    )

    assert memory_queue._manual_processor(
        {"id": "compile-id", "type": "compile", "payload": {}}
    ) is True
    assert calls == [
        ([sys.executable, str(script), "--trigger", "auto"], tmp_path.resolve())
    ]


def test_deferred_compile_failure_retries_then_dies(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_queue

    queue = MemoryQueue(tmp_path, clock=clock, rng=random.Random(1))
    task_id = queue.enqueue("compile", 1, {})
    calls: list[list[str]] = []

    def failed_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
    monkeypatch.setattr(memory_queue.subprocess, "run", failed_run)

    first = memory_queue.run_worker(
        memory_queue._manual_processor,
        max_tasks=1,
        idle_seconds=0,
        max_attempts=2,
        retry_base_seconds=1,
        retry_cap_seconds=1,
        processor_runner=memory_queue._run_processor_inline,
    )
    retry = queue.get(task_id)
    assert first.failed == 1
    assert retry.state == "ready"
    clock.advance((retry.available_at - clock()).total_seconds())

    second = memory_queue.run_worker(
        memory_queue._manual_processor,
        max_tasks=1,
        idle_seconds=0,
        max_attempts=2,
        retry_base_seconds=1,
        retry_cap_seconds=1,
        processor_runner=memory_queue._run_processor_inline,
    )
    dead = queue.get(task_id)
    assert second.failed == 1
    assert dead.state == "dead"
    assert dead.error_code == "processor_failed"
    assert len(calls) == 2
