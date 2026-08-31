"""Reclaim of a provably dead owner must take its projections with it.

Found by the MEM-18 durability stand (`docs/research/2026-08-28-zero-silent-loss-stand.md`)
and registered as NEW-113/114/115: one SIGKILLed process turned into a permanent
capture-processing outage because reclaim removed the owner row and nothing else.

The contract these tests hold to is the existing one, unweakened: a row is
reclaimable only when its lease has expired **and** the OS says its process is
gone. A live owner, or one whose liveness cannot be established, is still
refused by name. See `docs/research/2026-08-28-ownership-reclaim-under-process-death.md`.

The dead process here is real, not simulated by a probe: the owner row names
this process's pid with a start identity that is not this process's, so the
platform probe answers `dead` on every supported OS without any injection.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership as ownership  # noqa: E402
from project_journal import ProjectStore  # noqa: E402

INTENT_ID = "a" * 64
WORKER_SCOPE = "worker:capture-recovery"


def _candidate(state_root: Path) -> Path:
    return state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"


def _coordinator(state_root: Path):
    markdown_transaction.initialize_coordinator_v3_candidate(
        _candidate(state_root), source_v2=None
    )
    return markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        _candidate(state_root), state_root=state_root
    )


def _queue(state_root: Path):
    candidate = state_root / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(candidate, state_root=state_root)


def _dead_identity() -> ownership.ProcessIdentity:
    """This pid with a start identity that is not this process's start identity."""
    return ownership.ProcessIdentity(
        pid=os.getpid(), start_identity="llm-wiki-test:killed-process"
    )


def _run_as_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership, "current_process_identity", _dead_identity)


def _run_as_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()


def _expire(database_path: Path, table: str, *, seconds: int = 1) -> None:
    """Move a lease deadline into the past — timestamps only, never rows."""
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    stamp = when.isoformat().replace("+00:00", "Z")
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        database.execute(f"UPDATE {table} SET expires_at=?", (stamp,))
        database.commit()


def _count(database_path: Path, table: str) -> int:
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        return int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _shape_is_consistent(database_path: Path) -> bool:
    with contextlib.closing(sqlite3.connect(database_path)) as database:
        return markdown_transaction._coordinator_v3_base_cross_table_invariant(database)


def test_reclaim_of_a_dead_worker_removes_its_orphaned_intent_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-113: the fence's FOREIGN KEY made every later worker die at acquire."""
    coordinator = _coordinator(tmp_path)
    registry = ownership.OwnershipRegistry(tmp_path)
    _run_as_dead_process(monkeypatch)
    dead = registry.acquire("queue-worker", scope=WORKER_SCOPE)
    coordinator.acquire_intent_fence(INTENT_ID, mode="worker", owner=dead)
    _expire(_candidate(tmp_path), "maintenance_owners")
    _run_as_this_process(monkeypatch)

    successor = registry.acquire("queue-worker", scope=WORKER_SCOPE)

    assert successor.epoch == dead.epoch + 1
    assert _count(_candidate(tmp_path), "intent_fences") == 0
    assert _count(_candidate(tmp_path), "maintenance_owners") == 1
    assert _shape_is_consistent(_candidate(tmp_path))


def _sealed_capture_binding(queue, coordinator, registry):
    """The capture task and its sealed link, as the producer leaves them."""
    intent_path = f"run/capture-intents/{INTENT_ID}.json"
    queue.publish_capture_intent(
        intent_id=INTENT_ID,
        intent_path=intent_path,
        intent_sha256="e" * 64,
        byte_size=128,
    )
    owner = registry.acquire("capture", scope=f"intent:{INTENT_ID}")
    fence = coordinator.acquire_intent_fence(INTENT_ID, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "capture"},
        intent_id=INTENT_ID,
        intent_path=intent_path,
        intent_sha256="e" * 64,
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return queue.seal_capture_binding(
        binding.task_id,
        consumer_kind="transaction",
        consumer_id="transaction:fixture",
        active_link_digest=binding.active_digest,
    )


def test_reclaim_removes_the_binding_projection_of_the_fence_it_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding projection without its fence is a corrupt-shape verdict.

    This is the measured wedge: a worker killed between projecting the binding
    and committing the Markdown holds a worker-mode fence over both.
    """
    coordinator = _coordinator(tmp_path)
    queue = _queue(tmp_path)
    registry = ownership.OwnershipRegistry(tmp_path)
    sealed = _sealed_capture_binding(queue, coordinator, registry)
    _run_as_dead_process(monkeypatch)
    dead = registry.acquire("queue-worker", scope=WORKER_SCOPE)
    fence = coordinator.acquire_intent_fence(INTENT_ID, mode="worker", owner=dead)
    coordinator.project_capture_binding(sealed, intent_fence=fence)
    assert _count(_candidate(tmp_path), "capture_binding_projections") == 1
    _expire(_candidate(tmp_path), "maintenance_owners")
    _run_as_this_process(monkeypatch)

    registry.acquire("queue-worker", scope=WORKER_SCOPE)

    assert _count(_candidate(tmp_path), "capture_binding_projections") == 0
    assert _count(_candidate(tmp_path), "intent_fences") == 0
    assert _shape_is_consistent(_candidate(tmp_path))


def test_reclaim_removes_the_project_lease_projection_of_a_dead_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`project_leases` carries the same FOREIGN KEY the fence does."""
    vault = tmp_path / "vault"
    (vault / "knowledge" / "projects").mkdir(parents=True)
    _coordinator(tmp_path)
    store = ProjectStore._from_v3_candidate(vault, state_root=tmp_path)
    registry = ownership.OwnershipRegistry(tmp_path)
    _run_as_dead_process(monkeypatch)
    store.acquire_lease("demo", "agent-a", 30)
    _expire(_candidate(tmp_path), "maintenance_owners")
    _run_as_this_process(monkeypatch)

    successor = registry.acquire("project", scope="project:demo")

    assert successor.epoch == 2
    assert _count(_candidate(tmp_path), "project_leases") == 0
    assert _shape_is_consistent(_candidate(tmp_path))


def test_a_dead_row_under_another_role_no_longer_blocks_this_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-114: UNIQUE(actor_id) is one row per uid, so a dead capture row
    silenced queue-worker, compile, doctor, nightly and weekly for that user."""
    _coordinator(tmp_path)
    registry = ownership.OwnershipRegistry(tmp_path)
    _run_as_dead_process(monkeypatch)
    registry.acquire("capture", scope=f"intent:{INTENT_ID}", actor_id="posix-uid:7")
    _expire(_candidate(tmp_path), "maintenance_owners")
    _run_as_this_process(monkeypatch)

    successor = registry.acquire(
        "queue-worker", scope=WORKER_SCOPE, actor_id="posix-uid:7"
    )

    assert (successor.role, successor.scope) == ("queue-worker", WORKER_SCOPE)
    assert _count(_candidate(tmp_path), "maintenance_owners") == 1




def _acquire_conflicting_row(
    registry, monkeypatch: pytest.MonkeyPatch, *, dead: bool
) -> None:
    if dead:
        _run_as_dead_process(monkeypatch)
    registry.acquire("capture", scope=f"intent:{INTENT_ID}", actor_id="posix-uid:7")
    if dead:
        _run_as_this_process(monkeypatch)


def _expire_if(database_path: Path, expired: bool) -> None:
    if expired:
        _expire(database_path, "maintenance_owners")


@pytest.mark.parametrize(
    ("expired", "dead"),
    [(False, True), (True, False), (False, False)],
    ids=("dead-but-unexpired", "expired-but-alive", "live"),
)
def test_a_row_this_actor_cannot_prove_dead_still_refuses_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool, dead: bool
) -> None:
    """Both halves of the proof are still required; doubt refuses closed."""
    _coordinator(tmp_path)
    registry = ownership.OwnershipRegistry(tmp_path)
    _acquire_conflicting_row(registry, monkeypatch, dead=dead)
    _expire_if(_candidate(tmp_path), expired)

    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.acquire("queue-worker", scope=WORKER_SCOPE, actor_id="posix-uid:7")

    assert error.value.code == "owner_identity_conflict"
    assert _count(_candidate(tmp_path), "maintenance_owners") == 1


def _crashed_queue_owner(queue, scope: str):
    """The two writes `queue_owner` commits before its `finally` can run.

    A SIGKILL between them and the `finally` leaves exactly this: a canonical
    lease row and its queue projection, with nothing to remove either.
    """
    registry = queue.ownership_registry()
    lease = registry.acquire("queue-worker", scope=scope)
    queue._insert_queue_projection(lease, role="queue-worker", scope=scope)
    return registry, lease


def test_the_queue_projection_of_a_dead_worker_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-115: `queue_ownership` was insert-only, so no worker started again."""
    _coordinator(tmp_path)
    queue = _queue(tmp_path)
    _run_as_dead_process(monkeypatch)
    _crashed_queue_owner(queue, WORKER_SCOPE)
    _expire(_candidate(tmp_path), "maintenance_owners")
    _expire(queue.db_path, "queue_ownership")
    _run_as_this_process(monkeypatch)

    with queue.queue_owner(role="queue-worker", scope=WORKER_SCOPE) as successor:
        assert successor.role == "queue-worker"
        assert _count(queue.db_path, "queue_ownership") == 1

    assert _count(queue.db_path, "queue_ownership") == 0


def test_a_live_queue_projection_still_refuses_by_name(tmp_path: Path) -> None:
    """An unexpired projection of a running process is not reclaimable.

    Reached by releasing the canonical lease while its queue projection stands:
    the coordinator is free, the queue row is live, and this process is the one
    the row names — so neither half of the proof holds.
    """
    _coordinator(tmp_path)
    queue = _queue(tmp_path)
    registry, lease = _crashed_queue_owner(queue, WORKER_SCOPE)
    registry.release(lease)

    with pytest.raises(memory_queue.QueueOperationError) as error:
        with queue.queue_owner(role="queue-worker", scope=WORKER_SCOPE):
            pass

    assert error.value.code == "queue_owner_busy"
    assert _count(queue.db_path, "queue_ownership") == 1


def test_a_dead_workers_task_fence_is_reclaimed_with_its_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue holds a second projection of the same owner: the task fence.

    Reclaiming only `queue_ownership` clears the wedge one step and leaves the
    next one — `acquire_task_fence` refuses `task_fenced` on bare row
    existence, exactly as the intent fence refused before this change.
    """
    _coordinator(tmp_path)
    queue = _queue(tmp_path)
    task_id = queue.enqueue("query", 1, {"prompt": "ordinary"}, priority=7)
    _run_as_dead_process(monkeypatch)
    _, dead = _crashed_queue_owner(queue, WORKER_SCOPE)
    queue.acquire_task_fence(task_id, mode="worker", owner=dead)
    _expire(_candidate(tmp_path), "maintenance_owners")
    _expire(queue.db_path, "queue_ownership")
    _run_as_this_process(monkeypatch)

    with queue.queue_owner(role="queue-worker", scope=WORKER_SCOPE) as successor:
        assert _count(queue.db_path, "task_fences") == 0
        fence = queue.acquire_task_fence(task_id, mode="worker", owner=successor)
        assert fence.task_id == task_id
        queue.release_task_fence(fence)
