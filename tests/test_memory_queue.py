"""Behavior tests for the fenced SQLite deferred-work queue."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import stat
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from inspect import signature
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_queue import (  # noqa: E402
    LeaseFenceError,
    MemoryQueue,
    QueueFailure,
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
    assert list(parameters) == ["self", "owner", "lease_seconds"]
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
    queue.acknowledge(current)
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
    if sys.platform != "win32":
        assert stat.S_IMODE(result_path.stat().st_mode) == 0o600


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


def test_acknowledge_requires_published_result(queue: MemoryQueue) -> None:
    queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None
    with pytest.raises(ValueError, match="result"):
        queue.acknowledge(lease)


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
    assert task.attempt_history[-1].attempt == 8
    assert task.attempt_history[-1].error_code == "attempts_exhausted"


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
        "skipped": 0,
    }
    assert in_transaction == [False]
    assert memory_queue.list_pending() == []
    snapshot = memory_queue.status()
    assert snapshot["pending_total"] == 0
    assert snapshot["queue_dir"].endswith("run")


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
