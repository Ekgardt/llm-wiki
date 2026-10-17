"""A capture keeps every claim it holds while its classifier runs.

No capture that took longer than 30 seconds had ever succeeded: the intent fence
lived 30 s and nothing renewed it. Research:
`docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import flush_memory  # noqa: E402
import operational_ownership  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402
from tests.test_queue_v3_capture_links import _capture_binding, _coordinator, _queue  # noqa: E402

SCOPE = "worker:capture-keepalive"


def _held_claims(tmp_path: Path):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(queue, coordinator, registry, intent_id="5" * 64, intent_sha256="6" * 64)
    lease = queue.claim_capture("capture-worker")
    owner = registry.acquire("queue-worker", scope=SCOPE)
    return queue, coordinator, registry, binding, lease, owner


def _expiry(database: Path, table: str, key: str, value: str) -> str:
    with sqlite3.connect(database) as connection:
        return connection.execute(f"SELECT expires_at FROM {table} WHERE {key}=?", (value,)).fetchone()[0]


def test_both_fences_are_pushed_out_while_they_are_live(tmp_path):
    queue, coordinator, registry, binding, lease, owner = _held_claims(tmp_path)
    with queue.queue_owner(role="queue-worker", scope=SCOPE, parent=owner):
        task_fence = queue.acquire_task_fence(binding.task_id, mode="worker", owner=owner)
        intent_fence = coordinator.acquire_intent_fence(binding.intent_id, mode="worker", owner=owner)
        before = _expiry(coordinator.database_path, "intent_fences", "intent_id", binding.intent_id)
        renewed = queue.heartbeat_queue_owner(owner)
        queue.heartbeat_task_fence(task_fence, renewed)
        coordinator.heartbeat_intent_fence(intent_fence, renewed)
        after = _expiry(coordinator.database_path, "intent_fences", "intent_id", binding.intent_id)
        queue.release_task_fence(task_fence)
        coordinator.release_intent_fence(intent_fence)
    registry.release(owner)

    assert after > before


def test_a_released_fence_is_not_brought_back(tmp_path):
    queue, coordinator, registry, binding, lease, owner = _held_claims(tmp_path)
    with queue.queue_owner(role="queue-worker", scope=SCOPE, parent=owner):
        intent_fence = coordinator.acquire_intent_fence(binding.intent_id, mode="worker", owner=owner)
        coordinator.release_intent_fence(intent_fence)
        with pytest.raises(RuntimeError, match="intent_fence_lost"):
            coordinator.heartbeat_intent_fence(intent_fence, owner)
    registry.release(owner)


class _Recorder:
    def __init__(self, beats: threading.Event) -> None:
        self.beats = beats
        self.calls: list[str] = []

    def heartbeat_queue_owner(self, owner):
        self.calls.append("owner")
        return owner

    def heartbeat(self, lease):
        self.calls.append("lease")
        return lease

    def heartbeat_task_fence(self, fence, owner):
        self.calls.append("task")

    def heartbeat_intent_fence(self, fence, owner):
        self.calls.append("intent")
        self.beats.set()


def test_the_classifier_call_runs_under_renewals_of_every_claim(monkeypatch):
    beats = threading.Event()
    recorder = _Recorder(beats)
    monkeypatch.setattr(flush_memory, "CAPTURE_KEEPALIVE_SECONDS", 0.01)

    with flush_memory._CaptureKeepAlive(recorder, recorder, "lease", "task", "intent", "owner"):
        assert beats.wait(timeout=SHORT_TIMEOUT)

    assert recorder.calls[:4] == ["owner", "lease", "task", "intent"]


def test_a_tier_keeps_its_meaning_through_punctuation_and_an_explanation():
    parse = flush_memory._parse_capture_wire_output

    assert [parse("FLUSH_OK."), parse("FLUSH_OK\n\nNothing durable here.")] == [("ok", ""), ("ok", "")]
    assert parse("FLUSH_MAJOR: the decision\nand its reason") == ("major", "the decision\nand its reason")


def _advance_at_write_admission(monkeypatch, module, seconds=31):
    """Model time spent opening/locking SQLite without a scheduler-dependent sleep."""
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone

    clock = [datetime.now(timezone.utc)]
    original = module.begin_immediate

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    @contextmanager
    def admitted(database):
        with original(database):
            clock[0] += timedelta(seconds=seconds)
            yield

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "begin_immediate", admitted)
    return clock


def _fence_case(kind, queue, coordinator, binding):
    import markdown_transaction
    import memory_queue

    return {
        "intent": (markdown_transaction, coordinator, binding.intent_id, "intent_fences", "intent_id"),
        "task": (memory_queue, queue, binding.task_id, "task_fences", "task_id"),
    }[kind]


@pytest.mark.parametrize("kind", ["intent", "task"])
def test_an_expired_fence_cannot_be_renewed_after_waiting_for_write_admission(tmp_path, monkeypatch, kind):
    queue, coordinator, registry, binding, lease, owner = _held_claims(tmp_path)
    with queue.queue_owner(role="queue-worker", scope=SCOPE, parent=owner):
        module, store, identity, table, key = _fence_case(kind, queue, coordinator, binding)
        fence = getattr(store, f"acquire_{kind}_fence")(identity, mode="worker", owner=owner)
        path = {"intent": coordinator.database_path, "task": queue.db_path}[kind]
        before = _expiry(path, table, key, identity)
        _advance_at_write_admission(monkeypatch, module, getattr(module, f"{kind.upper()}_FENCE_SECONDS") + 1)
        with pytest.raises(RuntimeError, match="fence_lost"):
            getattr(store, f"heartbeat_{kind}_fence")(fence, owner)
        assert _expiry(path, table, key, identity) == before
        getattr(store, f"release_{kind}_fence")(fence)
    registry.release(owner)


@pytest.mark.parametrize("kind", ["intent"])
def test_new_fence_expiry_starts_at_write_admission(tmp_path, monkeypatch, kind):
    from datetime import timedelta

    queue, coordinator, registry, binding, lease, owner = _held_claims(tmp_path)
    with queue.queue_owner(role="queue-worker", scope=SCOPE, parent=owner):
        module, store, identity, table, key = _fence_case(kind, queue, coordinator, binding)
        clock = _advance_at_write_admission(monkeypatch, module)
        fence = getattr(store, f"acquire_{kind}_fence")(identity, mode="worker", owner=owner)
        seconds = getattr(module, f"{kind.upper()}_FENCE_SECONDS")
        assert fence.expires_at == min(owner.expires_at, clock[0] + timedelta(seconds=seconds))
        getattr(store, f"release_{kind}_fence")(fence)
    registry.release(owner)


def test_expired_intent_cannot_project_a_binding_after_write_admission(tmp_path, monkeypatch):
    import markdown_transaction

    queue, coordinator, registry, binding, lease, owner = _held_claims(tmp_path)
    with queue.queue_owner(role="queue-worker", scope=SCOPE, parent=owner):
        fence = coordinator.acquire_intent_fence(binding.intent_id, mode="worker", owner=owner)
        binding = queue.seal_capture_binding(binding.task_id, consumer_kind="terminal",
                                             consumer_id="expired-proof", active_link_digest=binding.active_digest)
        _advance_at_write_admission(monkeypatch, markdown_transaction)
        with pytest.raises(RuntimeError, match="intent_fence_lost"):
            coordinator.project_capture_binding(binding, intent_fence=fence)
        coordinator.release_intent_fence(fence)
    registry.release(owner)


def test_expired_queue_owner_cannot_acquire_a_task_fence_after_waiting(tmp_path, monkeypatch):
    import memory_queue

    queue, coordinator, registry, binding, lease, owner = _held_claims(tmp_path)
    with queue.queue_owner(role="queue-worker", scope=SCOPE, parent=owner):
        _advance_at_write_admission(monkeypatch, memory_queue, owner.ttl_seconds + 1)
        with pytest.raises(RuntimeError, match="queue_owner_fence_lost"):
            queue.acquire_task_fence(binding.task_id, mode="worker", owner=owner)
    registry.release(owner)
