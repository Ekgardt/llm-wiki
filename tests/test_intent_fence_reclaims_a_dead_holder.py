"""A dead holder's intent fence is reclaimed, not a permanent wedge.

`intent_fences.intent_id` is the primary key, so exactly one row can exist per
intent, and `acquire_intent_fence` refused on the mere presence of that row.
The row is keyed by `intent_id` — by neither `role` nor `scope` — so the
registry, which reclaims a dead owner by `(role, scope)`, cannot reach it from
a caller holding a different owner.

Measured on unmodified code 2026-08-29, three cases:

* a dead *capture publisher* holding its own fence is already adopted, 1 of 1:
  publisher and adopter both take `(capture, intent:<id>)`, so the registry's
  own reclaim deletes the fence as an owner projection. The hole reported when
  `NEW-136` closed does not exist in that shape;
* a dead *worker* holding a worker-mode fence on the same intent wedges it
  forever — `RuntimeError: intent_fenced`, and the row is still there after the
  pass. `capture_task_fences` hands the worker's own owner to a fence keyed by
  the intent, so a worker killed mid-capture-task leaves a row that adoption,
  which takes `(capture, intent:<id>)`, can never reach;
* a live worker is refused with the same name, which must stay true.
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

import capture_adoption  # noqa: E402
import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership as ownership  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    publish_runtime_file,
    sha256_bytes,
)

WORKER_SCOPE = "queue"


def _coordinator_path(state_root: Path) -> Path:
    return state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"


def _build(state_root: Path):
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    markdown_transaction.initialize_coordinator_v3_candidate(
        _coordinator_path(state_root), source_v2=None
    )
    memory_queue.initialize_queue_v3_candidate(
        state_root / "run" / "queue-v3.candidate.sqlite3", source_v2=None
    )
    queue = memory_queue.MemoryQueue._from_v3_candidate(
        state_root / "run" / "queue-v3.candidate.sqlite3", state_root=state_root
    )
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        _coordinator_path(state_root), state_root=state_root
    )
    return queue, coordinator


def _publish_ready_intent(state_root: Path, queue, coordinator) -> str:
    """Every publication step for real, up to but not including the enqueue."""
    seed = b"intent-fence-reclaim"
    payload = canonical_json_bytes({"seed": seed.decode()})
    intent_id = sha256_bytes(seed)
    shard = intent_id[:2]
    pending = f"run/capture-intents/pending/{shard}/{intent_id}.json"
    ready = f"run/capture-intents/ready/{shard}/{intent_id}.json"
    for relative in (pending, ready):
        (state_root / relative).parent.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "intent_id": intent_id,
        "pending_path": pending,
        "ready_path": ready,
        "intent_sha256": sha256_bytes(payload),
        "byte_size": len(payload),
    }
    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    publish_runtime_file(
        state_root / pending,
        payload,
        state_root=state_root,
        create_only=True,
        mode=0o600,
    )
    queue.index_capture_intent_pending(**descriptor)
    publish_runtime_file(
        state_root / ready,
        payload,
        state_root=state_root,
        create_only=True,
        mode=0o600,
    )
    queue.mark_capture_intent_ready(**descriptor)
    (state_root / pending).unlink()
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return intent_id


def _dead_identity() -> ownership.ProcessIdentity:
    return ownership.ProcessIdentity(
        pid=os.getpid(), start_identity="llm-wiki-test:killed-process"
    )


def _expire(state_root: Path, table: str) -> None:
    when = datetime.now(timezone.utc) - timedelta(seconds=1)
    stamp = when.isoformat().replace("+00:00", "Z")
    with contextlib.closing(sqlite3.connect(_coordinator_path(state_root))) as database:
        database.execute(f"UPDATE {table} SET expires_at=?", (stamp,))
        database.commit()


def _fence_row(state_root: Path) -> sqlite3.Row | None:
    with contextlib.closing(sqlite3.connect(_coordinator_path(state_root))) as database:
        database.row_factory = sqlite3.Row
        return database.execute("SELECT * FROM intent_fences").fetchone()


def _wedge(state_root: Path, monkeypatch: pytest.MonkeyPatch, *, dead: bool):
    """Leave a worker-mode fence on a ready intent, as an abrupt death does."""
    queue, coordinator = _build(state_root)
    intent_id = _publish_ready_intent(state_root, queue, coordinator)
    registry = queue.ownership_registry()
    if dead:
        monkeypatch.setattr(ownership, "current_process_identity", _dead_identity)
    worker = registry.acquire("queue-worker", scope=WORKER_SCOPE)
    coordinator.acquire_intent_fence(intent_id, mode="worker", owner=worker)
    if dead:
        monkeypatch.undo()
    return queue, coordinator, intent_id


def test_a_dead_holders_intent_fence_is_reclaimed_and_the_intent_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, coordinator, intent_id = _wedge(tmp_path, monkeypatch, dead=True)
    _expire(tmp_path, "intent_fences")
    _expire(tmp_path, "maintenance_owners")

    result = capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path
    )

    assert result["skipped"] == []
    assert [entry["intent_id"] for entry in result["adopted"]] == [intent_id]


def test_a_live_holder_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doubt refuses closed, and the refusal keeps the name callers report."""
    queue, coordinator, intent_id = _wedge(tmp_path, monkeypatch, dead=False)
    _expire(tmp_path, "intent_fences")

    result = capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path
    )

    assert result["adopted"] == []
    assert [entry["reason"] for entry in result["skipped"]] == [
        "RuntimeError: intent_fenced"
    ]
    assert _fence_row(tmp_path) is not None


def test_an_unexpired_dead_holder_is_not_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the proof are required; a live lease is not ours to take."""
    queue, coordinator, _ = _wedge(tmp_path, monkeypatch, dead=True)

    result = capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path
    )

    assert result["adopted"] == []
    assert [entry["reason"] for entry in result["skipped"]] == [
        "RuntimeError: intent_fenced"
    ]


def test_the_reclaim_takes_the_binding_projection_with_the_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding without its fence is a shape violation the coordinator counts."""
    queue, coordinator, intent_id = _wedge(tmp_path, monkeypatch, dead=True)
    fence = _fence_row(tmp_path)
    with contextlib.closing(sqlite3.connect(_coordinator_path(tmp_path))) as database:
        database.execute(
            """INSERT INTO capture_binding_projections(
                   intent_id,task_id,active_link_digest,seal_digest,projected_at,
                   intent_fence_token,intent_fence_epoch
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                intent_id,
                "task-wedged",
                "b" * 64,
                "c" * 64,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                fence["token"],
                fence["fencing_epoch"],
            ),
        )
        database.commit()
    _expire(tmp_path, "intent_fences")
    _expire(tmp_path, "maintenance_owners")

    result = capture_adoption.adopt_orphaned_capture_intents(
        queue, coordinator, state_root=tmp_path
    )

    assert [entry["intent_id"] for entry in result["adopted"]] == [intent_id]
    with contextlib.closing(sqlite3.connect(_coordinator_path(tmp_path))) as database:
        database.row_factory = sqlite3.Row
        assert markdown_transaction._coordinator_v3_base_cross_table_invariant(database)
        assert (
            database.execute(
                "SELECT COUNT(*) FROM capture_binding_projections WHERE task_id=?",
                ("task-wedged",),
            ).fetchone()[0]
            == 0
        )
