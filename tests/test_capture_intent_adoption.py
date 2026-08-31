"""A capture intent published but never dispatched must still reach a worker.

The publisher writes the durable intent and only then enqueues the task that
gives it a worker. Lose the fence or the owner in between and the intent is
committed with no task, and nothing looks for it: `recover_expired_leases`
recovers a task whose lease expired, which presupposes a task.

See `docs/research/2026-08-28-adopting-an-orphaned-intent.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capture_adoption  # noqa: E402
import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    publish_runtime_file,
    sha256_bytes,
)


def _queue(tmp_path: Path):
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    markdown_transaction.initialize_coordinator_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3", source_v2=None
    )
    memory_queue.initialize_queue_v3_candidate(
        tmp_path / "run" / "queue-v3.candidate.sqlite3", source_v2=None
    )
    return memory_queue.MemoryQueue._from_v3_candidate(
        tmp_path / "run" / "queue-v3.candidate.sqlite3", state_root=tmp_path
    )


def _coordinator(tmp_path: Path):
    return markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3",
        state_root=tmp_path,
    )


def _publish_ready_intent(tmp_path: Path, queue, coordinator, seed: bytes) -> dict:
    """Every publication step for real, up to but not including the enqueue."""
    payload = canonical_json_bytes({"seed": seed.decode()})
    digest = sha256_bytes(payload)
    intent_id = sha256_bytes(seed)
    shard = intent_id[:2]
    pending = f"run/capture-intents/pending/{shard}/{intent_id}.json"
    ready = f"run/capture-intents/ready/{shard}/{intent_id}.json"
    for relative in (pending, ready):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)

    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    descriptor = {
        "intent_id": intent_id,
        "pending_path": pending,
        "ready_path": ready,
        "intent_sha256": digest,
        "byte_size": len(payload),
    }
    publish_runtime_file(
        tmp_path / pending, payload, state_root=tmp_path, create_only=True, mode=0o600
    )
    queue.index_capture_intent_pending(**descriptor)
    publish_runtime_file(
        tmp_path / ready, payload, state_root=tmp_path, create_only=True, mode=0o600
    )
    queue.mark_capture_intent_ready(**descriptor)
    (tmp_path / pending).unlink()
    # The loss: the fence and the owner are gone before the task is enqueued.
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return {
        "intent_id": intent_id,
        "ready": ready,
        "sha256": digest,
        "fence_token": fence.token,
        "fence_epoch": fence.epoch,
    }


def _links(queue) -> list[sqlite3.Row]:
    database = sqlite3.connect(str(queue.db_path))
    database.row_factory = sqlite3.Row
    try:
        return database.execute("SELECT * FROM capture_task_links").fetchall()
    finally:
        database.close()


def _adopted_ids(result: dict) -> list[str]:
    return [entry["intent_id"] for entry in result["adopted"]]


def _skipped_ids(result: dict) -> list[str]:
    return [entry["intent_id"] for entry in result["skipped"]]


def _linked_ids(queue) -> list[str]:
    return [row["intent_id"] for row in _links(queue)]


def _orphan_ids(queue) -> list[str]:
    return [
        str(record["intent_id"])
        for record in queue.ready_capture_intents_without_task(32)
    ]


def _adopt(queue, coordinator, tmp_path: Path, **kwargs) -> dict:
    return capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path, **kwargs
    )


def _damage(tmp_path: Path, orphan: dict) -> None:
    target = tmp_path / str(orphan["ready"])
    target.chmod(0o600)
    target.write_bytes(canonical_json_bytes({"seed": "tampered"}))


def test_the_orphan_this_recovers_from_is_real(tmp_path: Path) -> None:
    """Guard the premise: publication really can leave an intent with no task."""
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    orphan = _publish_ready_intent(tmp_path, queue, coordinator, b"orphan-premise")

    assert (
        _orphan_ids(queue),
        _linked_ids(queue),
        (tmp_path / str(orphan["ready"])).exists(),
    ) == ([orphan["intent_id"]], [], True)


def test_a_ready_intent_with_no_task_is_adopted(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    orphan = _publish_ready_intent(tmp_path, queue, coordinator, b"adopt-me")

    result = _adopt(queue, coordinator, tmp_path)

    # Recovered means: it has a task, and the orphan query no longer sees it.
    assert (
        result["skipped"],
        _adopted_ids(result),
        _linked_ids(queue),
        _orphan_ids(queue),
    ) == ([], [orphan["intent_id"]], [orphan["intent_id"]], [])


def test_adopting_twice_produces_one_task(tmp_path: Path) -> None:
    """The dedupe key is the mechanism; this proves it rather than assuming it."""
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    _publish_ready_intent(tmp_path, queue, coordinator, b"adopt-twice")

    first = _adopt(queue, coordinator, tmp_path)
    second = _adopt(queue, coordinator, tmp_path)

    # The second pass finds nothing to do, and one task exists either way.
    assert (
        len(first["adopted"]),
        second["adopted"],
        second["examined"],
        len(_links(queue)),
    ) == (1, [], 0, 1)


def test_adopting_the_same_intent_twice_directly_reuses_the_one_task(
    tmp_path: Path,
) -> None:
    """Force the second adoption past the orphan query, straight at the enqueue."""
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    _publish_ready_intent(tmp_path, queue, coordinator, b"adopt-directly-twice")
    records = queue.ready_capture_intents_without_task(32)

    first = capture_adoption._adopt_one(queue, coordinator, tmp_path, records[0])
    second = capture_adoption._adopt_one(queue, coordinator, tmp_path, records[0])

    assert (first == second, len(_links(queue))) == (True, 1)


def _assert_a_dead_fence_is_still_refused(queue, coordinator, orphan: dict) -> None:
    """The check that refused the publisher is not relaxed anywhere by adoption."""
    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{orphan['intent_id']}")
    fence = coordinator.acquire_intent_fence(
        str(orphan["intent_id"]), mode="capture", owner=owner
    )
    coordinator.release_intent_fence(fence)
    payload = {
        "intent_id": orphan["intent_id"],
        "intent_path": orphan["ready"],
        "intent_sha256": orphan["sha256"],
    }
    with pytest.raises(memory_queue.QueueOperationError, match="intent_fence_lost"):
        queue.enqueue_capture_task_replay_safe(
            "flush",
            1,
            payload,
            intent_id=str(orphan["intent_id"]),
            intent_path=str(orphan["ready"]),
            intent_sha256=str(orphan["sha256"]),
            capture_fence=fence,
            owner=owner,
        )
    registry.release(owner)


def test_adoption_takes_a_fresh_fence_rather_than_reusing_the_lost_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    orphan = _publish_ready_intent(tmp_path, queue, coordinator, b"fresh-fence")
    _assert_a_dead_fence_is_still_refused(queue, coordinator, orphan)

    taken: list[tuple] = []
    original = coordinator.acquire_intent_fence

    def _record(intent_id: str, *, mode: str, owner: object):
        fence = original(intent_id, mode=mode, owner=owner)
        taken.append((fence, owner))
        return fence

    monkeypatch.setattr(coordinator, "acquire_intent_fence", _record)
    result = _adopt(queue, coordinator, tmp_path)

    fence, owner = taken[-1]
    # A fresh fence and a fresh owner: this pass's own, not the publisher's.
    assert (
        len(result["adopted"]),
        len(taken),
        fence.token != orphan["fence_token"],
        fence.epoch > orphan["fence_epoch"],
        (owner.role, owner.scope),
    ) == (1, 1, True, True, ("capture", f"intent:{orphan['intent_id']}"))


def test_a_record_whose_bytes_moved_is_skipped_not_enqueued(tmp_path: Path) -> None:
    """A damaged record becomes a named skip, never a task that fails at the worker."""
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    orphan = _publish_ready_intent(tmp_path, queue, coordinator, b"moved-bytes")
    _damage(tmp_path, orphan)

    result = _adopt(queue, coordinator, tmp_path)

    assert (
        result["adopted"],
        _skipped_ids(result),
        _links(queue),
        "intent_digest_changed" in result["skipped"][0]["reason"],
    ) == ([], [orphan["intent_id"]], [], True)


def test_a_missing_record_is_skipped_not_enqueued(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    orphan = _publish_ready_intent(tmp_path, queue, coordinator, b"missing-record")
    (tmp_path / str(orphan["ready"])).unlink()

    result = _adopt(queue, coordinator, tmp_path)

    assert (result["adopted"], _skipped_ids(result), _links(queue)) == (
        [],
        [orphan["intent_id"]],
        [],
    )


def test_one_bad_record_does_not_stop_the_pass(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    bad = _publish_ready_intent(tmp_path, queue, coordinator, b"bad-one")
    good = _publish_ready_intent(tmp_path, queue, coordinator, b"good-one")
    _damage(tmp_path, bad)

    result = _adopt(queue, coordinator, tmp_path)

    assert (_adopted_ids(result), _skipped_ids(result)) == (
        [good["intent_id"]],
        [bad["intent_id"]],
    )


def test_the_pass_is_bounded(tmp_path: Path) -> None:
    """The cap is a named constant, and it is the cap the pass actually obeys."""
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    for index in range(3):
        _publish_ready_intent(tmp_path, queue, coordinator, f"bounded-{index}".encode())

    result = _adopt(queue, coordinator, tmp_path, limit=2)

    assert (
        result["examined"],
        len(result["adopted"]),
        len(_orphan_ids(queue)),
        capture_adoption.MAX_ADOPTED_INTENTS_PER_PASS,
    ) == (2, 2, 1, 32)


def test_the_orphan_query_ignores_intents_that_already_have_a_task(
    tmp_path: Path,
) -> None:
    """A succeeded task is not an orphan — the miscount NEW-136 was built on."""
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    _publish_ready_intent(tmp_path, queue, coordinator, b"already-linked")
    _adopt(queue, coordinator, tmp_path)
    database = sqlite3.connect(str(queue.db_path))
    try:
        database.execute("UPDATE tasks SET state='succeeded'")
        database.commit()
    finally:
        database.close()

    assert _orphan_ids(queue) == []


def test_the_capture_worker_adopts_before_it_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiring proof: the worker's own entry point recovers the orphan."""
    import flush_memory

    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    orphan = _publish_ready_intent(tmp_path, queue, coordinator, b"worker-adopts")
    monkeypatch.setattr(flush_memory, "STATE_ROOT", tmp_path)

    flush_memory.run_capture_worker_once(
        queue, coordinator, process_missing=lambda *args: None
    )

    assert _linked_ids(queue) == [orphan["intent_id"]]


def test_a_failing_sweeper_never_stops_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import flush_memory

    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    monkeypatch.setattr(flush_memory, "STATE_ROOT", tmp_path)

    def _explode(*args, **kwargs):
        raise RuntimeError("sweeper down")

    monkeypatch.setattr(
        capture_adoption, "adopt_orphaned_capture_intents", _explode
    )

    assert (
        flush_memory.run_capture_worker_once(
            queue, coordinator, process_missing=lambda *args: None
        )
        is None
    )


def test_a_queue_without_the_reader_is_reported_not_raised(tmp_path: Path) -> None:
    result = _adopt(object(), object(), tmp_path)
    assert (result["reason"], result["adopted"]) == ("unsupported", [])


def test_the_reader_refuses_a_meaningless_limit(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        queue.ready_capture_intents_without_task(0)
