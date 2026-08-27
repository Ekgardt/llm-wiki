"""The two named-refusal gaps from 2026-08-27, implemented for real.

The adopted V3 queue used to refuse the whole source-fence family
(`queue_api_not_adopted`) and the module-level queue owner registry
(`queue_tombstoned_by_adoption`) by name, because faking either would have
been worse. These tests build a genuinely adopted state root with the real
adoption command and prove both now work against the V3 schema the adoption
already ships: `source_fences` rows carry `logical_path` and
`owner_start_identity`, and the bounded worker's owner goes through the
canonical V3 ownership registry with a `queue_ownership` projection.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from archive_daily import DailyArchiver  # noqa: E402
from installed_memory_repair import repair_installed_vault  # noqa: E402
from reliable_memory import canonical_json_bytes, sha256_bytes  # noqa: E402

_DAILY = "2026-08-26"
_DIGEST = "b" * 64
_NOW = "2026-07-14T12:00:00.000000+00:00"


def _adopted_queue(
    tmp_path: Path,
) -> tuple[Path, Path, memory_queue._QueueV3CandidateReader]:
    """A vault whose queue and coordinator went through real V3 adoption."""
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    assert report["overall_status"] == "ok"
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    assert isinstance(queue, memory_queue._QueueV3CandidateReader)
    return root, state_root, queue


def _rows(state_root: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608


def _fence_snapshot(state_root: Path) -> list[tuple[str, str, str, int, bool]]:
    """Every stored fence as comparable identity fields."""
    return [
        (
            str(row["logical_path"]),
            str(row["source_digest"]),
            str(row["token"]),
            int(row["owner_pid"]),
            len(str(row["owner_start_identity"])) > 0,
        )
        for row in _rows(state_root, "source_fences")
    ]


def _owner_snapshots(
    state_root: Path,
) -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str]]]:
    """The queue_ownership projection and the canonical registry rows."""
    projection = [
        (
            str(row["canonical_role"]),
            str(row["domain_role"]),
            str(row["owner_token"]),
            int(row["process_id"]),
        )
        for row in _rows(state_root, "queue_ownership")
    ]
    database = state_root / "run/markdown-transactions-v3.sqlite3"
    with sqlite3.connect(database) as connection:
        canonical = connection.execute(
            "SELECT role, scope FROM maintenance_owners"
        ).fetchall()
    return projection, [tuple(row) for row in canonical]


def _acquire_worker_owner(state_root: Path) -> memory_queue.QueueOwnerLease:
    """The exact call doctor's `_run_bounded_worker` makes."""
    return memory_queue._acquire_queue_owner(
        state_root, "worker", "worker_busy", ttl_seconds=120
    )


def _inject_ready_task(
    state_root: Path, task_id: str, payload: dict[str, object]
) -> None:
    """A ready task written past the API, the way the legacy fence tests do."""
    blob = canonical_json_bytes(payload)
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_blob, input_hash, state,
                   priority, created_at, updated_at, available_at
               ) VALUES (?, 'compile', 1, ?, ?, 'ready', 0, ?, ?, ?)""",
            (task_id, blob, sha256_bytes(blob), _NOW, _NOW, _NOW),
        )


def test_the_fence_row_lives_in_the_adopted_schema(tmp_path: Path) -> None:
    _root, state_root, queue = _adopted_queue(tmp_path)

    fence = queue.acquire_source_fence(_DAILY, _DIGEST, lease_seconds=60)

    assert _fence_snapshot(state_root) == [
        (f"knowledge/daily/{_DAILY}.md", _DIGEST, fence.token, os.getpid(), True)
    ]

    renewed = queue.heartbeat_source_fence(fence, lease_seconds=120)
    assert (renewed.token, renewed.expires_at >= fence.expires_at) == (
        fence.token,
        True,
    )

    queue.release_source_fence(renewed.token)
    assert _fence_snapshot(state_root) == []


def test_a_second_fence_over_the_same_source_is_refused(tmp_path: Path) -> None:
    _root, _state_root, queue = _adopted_queue(tmp_path)
    queue.acquire_source_fence(_DAILY, _DIGEST)

    with pytest.raises(memory_queue.QueueOperationError, match="source_fenced"):
        queue.acquire_source_fence(_DAILY, _DIGEST)


def test_a_referenced_source_cannot_be_fenced(tmp_path: Path) -> None:
    _root, _state_root, queue = _adopted_queue(tmp_path)
    queue.enqueue("compile", 1, {"daily_id": _DAILY, "source_digest": _DIGEST})

    with pytest.raises(memory_queue.QueueOperationError, match="source_referenced"):
        queue.acquire_source_fence(_DAILY, _DIGEST)


def test_source_fence_atomically_blocks_enqueue_claim_and_redrive(
    tmp_path: Path,
) -> None:
    """The legacy fence contract, kept verbatim on the adopted queue."""
    _root, state_root, queue = _adopted_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAILY, _DIGEST)

    with pytest.raises(memory_queue.QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"daily_id": _DAILY})
    with pytest.raises(memory_queue.QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"source_digest": _DIGEST})

    # An external writer cannot make fenced work claimable.
    _inject_ready_task(state_root, "injected", {"daily_id": _DAILY})
    assert queue.claim("worker") is None

    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.execute("UPDATE tasks SET state='dead' WHERE id='injected'")
    with pytest.raises(memory_queue.QueueOperationError, match="source_fenced"):
        queue.redrive("injected")

    queue.release_source_fence(fence.token)
    assert queue.enqueue("compile", 1, {"daily_id": _DAILY})


def test_referencing_source_tasks_reads_the_payload_blob(tmp_path: Path) -> None:
    _root, _state_root, queue = _adopted_queue(tmp_path)
    referencing = queue.enqueue("compile", 1, {"daily_id": _DAILY})
    queue.enqueue("compile", 1, {"daily_id": "2000-01-01"})

    assert queue.referencing_source_tasks(_DAILY, _DIGEST) == (referencing,)
    assert queue.referencing_source_tasks("2026-08-25", "c" * 64) == ()


def test_source_finalization_rejects_expired_fence(tmp_path: Path) -> None:
    _root, state_root, queue = _adopted_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAILY, _DIGEST)
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as connection:
        connection.execute(
            "UPDATE source_fences SET expires_at=? WHERE token=?",
            ("2000-01-01T00:00:00+00:00", fence.token),
        )

    with pytest.raises(memory_queue.QueueOperationError, match="source_fence_lost"):
        with queue.source_finalization(fence):
            pytest.fail("expired fence must not finalize")


def test_source_finalization_rejects_task_injected_after_fence(
    tmp_path: Path,
) -> None:
    _root, state_root, queue = _adopted_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAILY, _DIGEST)
    _inject_ready_task(
        state_root,
        "injected-finalization",
        {"daily_id": _DAILY, "source_digest": _DIGEST},
    )

    with pytest.raises(memory_queue.QueueOperationError, match="source_referenced"):
        with queue.source_finalization(fence):
            pytest.fail("referenced source must not finalize")


def test_source_finalization_rejects_recorded_source_failure(
    tmp_path: Path,
) -> None:
    _root, _state_root, queue = _adopted_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAILY, _DIGEST)
    queue.record_source_failure(
        f"knowledge/daily/{_DAILY}.md",
        _DIGEST,
        error_code="ValueError",
        producer="compile",
    )

    with pytest.raises(memory_queue.QueueOperationError, match="source_failure"):
        with queue.source_finalization(fence):
            pytest.fail("failed source must not finalize")


def test_a_live_fence_with_no_references_finalizes(tmp_path: Path) -> None:
    _root, _state_root, queue = _adopted_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAILY, _DIGEST)

    finalized = False
    with queue.source_finalization(fence):
        finalized = True
    assert finalized

    queue.release_source_fence(fence.token)


def test_the_fence_heartbeat_context_keeps_the_fence_alive(tmp_path: Path) -> None:
    _root, _state_root, queue = _adopted_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAILY, _DIGEST, lease_seconds=60)

    with queue.source_fence_heartbeat(
        fence, heartbeat_seconds=1, lease_seconds=60
    ) as heartbeat:
        renewed = heartbeat.refresh()
        assert renewed.expires_at >= fence.expires_at

    queue.release_source_fence(renewed.token)


def test_the_archiver_queue_path_answers_instead_of_refusing(
    tmp_path: Path,
) -> None:
    """`archive_daily` was the dead caller: its fence and reference reads work."""
    root, state_root, _queue = _adopted_queue(tmp_path)
    (root / "knowledge/daily").mkdir(parents=True)
    archiver = DailyArchiver(root, state_root)
    assert isinstance(archiver.queue, memory_queue._QueueV3CandidateReader)

    assert archiver._queue_references(_DAILY, _DIGEST) == []

    fence = archiver.queue.acquire_source_fence(_DAILY, _DIGEST, lease_seconds=60)
    archiver.queue.release_source_fence(fence.token)


def test_the_bounded_worker_owner_acquires_on_an_adopted_vault(
    tmp_path: Path,
) -> None:
    _root, state_root, _queue = _adopted_queue(tmp_path)

    owner = _acquire_worker_owner(state_root)

    assert _owner_snapshots(state_root) == (
        [("queue-worker", "worker", owner.token, os.getpid())],
        [("queue-worker", "queue-owner:worker")],
    )

    assert memory_queue._release_queue_owner(owner) is True
    assert _owner_snapshots(state_root) == ([], [])


def test_the_worker_owner_heartbeats_and_excludes_a_second_worker(
    tmp_path: Path,
) -> None:
    _root, state_root, _queue = _adopted_queue(tmp_path)
    owner = _acquire_worker_owner(state_root)

    renewed = memory_queue._heartbeat_queue_owner(owner)
    assert (renewed.token, renewed.expires_at >= owner.expires_at) == (
        owner.token,
        True,
    )

    with pytest.raises(memory_queue.MigrationBusy) as busy:
        _acquire_worker_owner(state_root)
    assert busy.value.code == "worker_busy"

    assert memory_queue._release_queue_owner(renewed) is True


def test_legacy_owner_roles_keep_their_named_refusal_when_adopted(
    tmp_path: Path,
) -> None:
    """'legacy' and 'migration' guard pre-adoption workflows and stay refused."""
    _root, state_root, _queue = _adopted_queue(tmp_path)

    for role, busy_code in (
        ("legacy", "legacy_owner_busy"),
        ("migration", "migration_busy"),
    ):
        with pytest.raises(memory_queue.QueueOperationError) as raised:
            memory_queue._acquire_queue_owner(state_root, role, busy_code)
        assert raised.value.code == "queue_tombstoned_by_adoption"


def test_the_legacy_owner_registry_is_untouched_off_adoption(
    tmp_path: Path,
) -> None:
    """On an unadopted vault the registry keeps its pre-adoption database."""
    state_root = tmp_path / "plain-state"
    state_root.mkdir(parents=True)

    owner = memory_queue._acquire_queue_owner(state_root, "worker", "worker_busy")
    renewed = memory_queue._heartbeat_queue_owner(owner)
    assert memory_queue._release_queue_owner(renewed) is True

    with sqlite3.connect(state_root / "run/queue.sqlite3") as connection:
        stored = connection.execute(
            "SELECT role, token FROM queue_ownership"
        ).fetchall()
    assert stored == [("worker", None)]
