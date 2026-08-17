from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
from installed_memory_repair import repair_installed_vault  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    publish_runtime_file,
    sha256_bytes,
)


def _queue(tmp_path: Path):
    coordinator = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(
        coordinator, source_v2=None
    )
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(
        candidate, state_root=tmp_path
    )


def _coordinator(tmp_path: Path):
    return markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3",
        state_root=tmp_path,
    )


def _adopted_vault(tmp_path: Path) -> tuple[Path, Path]:
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
    return root, state_root


def test_active_capture_queue_uses_the_validated_adopted_pair(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)

    queue = memory_queue.active_memory_queue(root, state_root)
    coordinator = markdown_transaction.active_markdown_coordinator(root, state_root)
    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope="intent:fixture")

    assert queue.db_path == state_root / "run/queue-v3.sqlite3"
    assert coordinator.database_path == (
        state_root / "run/markdown-transactions-v3.sqlite3"
    )
    assert registry.database_path == coordinator.database_path
    registry.release(owner)


def test_capture_claim_ignores_unlinked_tasks(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    queue.enqueue("query", 1, {"prompt": "ordinary"}, priority=100)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="f" * 64,
        intent_sha256="e" * 64,
    )

    lease = queue.claim_capture("capture-worker")

    assert lease is not None
    assert lease.id == binding.task_id
    assert lease.kind == "flush"


def _capture_binding(
    queue, coordinator, registry, *, intent_id: str, intent_sha256: str
):
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "capture"},
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return binding


def test_capture_enqueue_atomically_inserts_task_and_immutable_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    intent_id = "1" * 64
    intent_sha256 = "2" * 64
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=f"run/capture-intents/{intent_id}.json",
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    capture_fence = coordinator.acquire_intent_fence(
        intent_id, mode="capture", owner=owner
    )
    real_insert = queue._insert_capture_link

    def fail_link(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected link insert failure")

    monkeypatch.setattr(queue, "_insert_capture_link", fail_link)
    with pytest.raises(RuntimeError, match="injected link insert failure"):
        queue.enqueue_capture_task(
            "flush",
            3,
            {"prompt": "capture"},
            intent_id=intent_id,
            intent_path=f"run/capture-intents/{intent_id}.json",
            intent_sha256=intent_sha256,
            capture_fence=capture_fence,
            owner=owner,
        )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert database.execute(
            "SELECT COUNT(*) FROM capture_task_links"
        ).fetchone() == (0,)

    monkeypatch.setattr(queue, "_insert_capture_link", real_insert)
    binding = queue.enqueue_capture_task(
        "flush",
        3,
        {"prompt": "capture"},
        intent_id=intent_id,
        intent_path=f"run/capture-intents/{intent_id}.json",
        intent_sha256=intent_sha256,
        capture_fence=capture_fence,
        owner=owner,
    )
    expected_digest = sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "capture-task-link/v1",
                "task_id": binding.task_id,
                "intent_id": intent_id,
                "intent_sha256": intent_sha256,
                "handler_version": 3,
            }
        )
    )
    assert binding == memory_queue.CaptureTaskBinding(
        task_id=binding.task_id,
        intent_id=intent_id,
        intent_sha256=intent_sha256,
        handler_version=3,
        active_digest=expected_digest,
        seal_digest=None,
    )
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone() == (1,)
        assert database.execute(
            """SELECT intent_id,intent_sha256,handler_version,link_digest
               FROM capture_task_links WHERE task_id=?""",
            (binding.task_id,),
        ).fetchone() == (intent_id, intent_sha256, 3, expected_digest)
    coordinator.release_intent_fence(capture_fence)
    registry.release(owner)


def test_capture_enqueue_rejects_missing_or_stale_capture_fence(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    intent_id = "5" * 64
    intent_sha256 = "6" * 64
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    coordinator.release_intent_fence(fence)

    with pytest.raises(memory_queue.QueueOperationError, match="intent_fence_lost"):
        queue.enqueue_capture_task(
            "flush",
            1,
            {"prompt": "capture"},
            intent_id=intent_id,
            intent_path=intent_path,
            intent_sha256=intent_sha256,
            capture_fence=fence,
            owner=owner,
        )

    with sqlite3.connect(queue.db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
    registry.release(owner)


def test_capture_task_fences_acquire_task_first_and_release_intent_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    intent_id = "3" * 64
    intent_sha256 = "4" * 64
    queue.publish_capture_intent(
        intent_id=intent_id,
        intent_path=f"run/capture-intents/{intent_id}.json",
        intent_sha256=intent_sha256,
        byte_size=128,
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    capture_owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    capture_fence = coordinator.acquire_intent_fence(
        intent_id, mode="capture", owner=capture_owner
    )
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "capture"},
        intent_id=intent_id,
        intent_path=f"run/capture-intents/{intent_id}.json",
        intent_sha256=intent_sha256,
        capture_fence=capture_fence,
        owner=capture_owner,
    )
    coordinator.release_intent_fence(capture_fence)
    registry.release(capture_owner)
    owner = registry.acquire("queue-worker", scope="worker:capture")
    events: list[str] = []
    real_task_acquire = queue.acquire_task_fence
    real_intent_acquire = coordinator.acquire_intent_fence
    real_task_release = queue.release_task_fence
    real_intent_release = coordinator.release_intent_fence

    def acquire_task(*args, **kwargs):
        events.append("acquire-task")
        return real_task_acquire(*args, **kwargs)

    def acquire_intent(*args, **kwargs):
        events.append("acquire-intent")
        return real_intent_acquire(*args, **kwargs)

    def release_task(fence):
        events.append("release-task")
        return real_task_release(fence)

    def release_intent(fence):
        events.append("release-intent")
        return real_intent_release(fence)

    monkeypatch.setattr(queue, "acquire_task_fence", acquire_task)
    monkeypatch.setattr(coordinator, "acquire_intent_fence", acquire_intent)
    monkeypatch.setattr(queue, "release_task_fence", release_task)
    monkeypatch.setattr(coordinator, "release_intent_fence", release_intent)

    with queue.queue_owner(
        role="queue-worker", scope="worker:capture", parent=owner
    ):
        with memory_queue.capture_task_fences(
            queue,
            coordinator,
            binding.task_id,
            intent_id=intent_id,
            mode="worker",
            owner=owner,
        ) as (task_fence, intent_fence):
            assert intent_fence is not None
            assert task_fence.owner == owner
            assert intent_fence.owner == owner
            with sqlite3.connect(queue.db_path) as database:
                assert database.execute(
                    """SELECT canonical_owner_token,canonical_fencing_epoch
                       FROM task_fences WHERE task_id=?""",
                    (binding.task_id,),
                ).fetchone() == (owner.token, owner.epoch)
            with sqlite3.connect(coordinator.database_path) as database:
                assert database.execute(
                    """SELECT canonical_owner_token,canonical_fencing_epoch
                       FROM intent_fences WHERE intent_id=?""",
                    (intent_id,),
                ).fetchone() == (owner.token, owner.epoch)

    assert events == [
        "acquire-task",
        "acquire-intent",
        "release-intent",
        "release-task",
    ]
    registry.release(owner)


def test_first_consumer_seals_active_digest_before_side_effect(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="7" * 64,
        intent_sha256="8" * 64,
    )
    operator = registry.acquire("repair", scope="repair:link-race")
    observed: list[str] = []
    start_resolution = threading.Event()
    resolution_finished = threading.Event()

    def resolve() -> None:
        start_resolution.wait(2)
        try:
            queue.append_capture_link_resolution(
                binding.task_id,
                supersedes_digest=binding.active_digest,
                observed={
                    "link_digest": binding.active_digest,
                    "index_digest": binding.active_digest,
                    "intent_file_digest": binding.intent_sha256,
                },
                selected_intent=None,
                owner=operator,
                reason="Attempt to supersede after consumption.",
            )
        except memory_queue.QueueOperationError as error:
            observed.append(error.code)
        finally:
            resolution_finished.set()

    with queue.queue_owner(
        role="queue-operator", scope="task:link-race", parent=operator
    ):
        thread = threading.Thread(target=resolve)
        thread.start()

        sealed = queue.seal_capture_binding(
            binding.task_id,
            consumer_kind="terminal",
            consumer_id="terminal:fixture",
            active_link_digest=binding.active_digest,
            before_side_effect=lambda: start_resolution.set(),
        )
        assert resolution_finished.wait(2)
        thread.join(timeout=2)

    assert sealed.seal_digest is not None
    assert observed == ["capture_link_sealed"]
    assert queue.active_capture_binding(None, binding.task_id) == sealed
    registry.release(operator)


@pytest.mark.parametrize("stage", ["flush", "feedback", "feedback-verify"])
def test_semantic_decision_seal_and_index_commit_together_without_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_id = {"flush": "9", "feedback": "a", "feedback-verify": "b"}[stage] * 64
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id=intent_id,
        intent_sha256="c" * 64,
    )
    lease = queue.claim("worker")
    assert lease is not None and lease.id == binding.task_id
    decision = canonical_json_bytes(
        {"schema_version": "semantic-decision/v1", "stage": stage, "value": "retain"}
    )
    decision_key = sha256_bytes(
        canonical_json_bytes({"intent_id": intent_id, "stage": stage})
    )
    decision_path = f"run/queue-results/capture-decision-{decision_key}.json"
    target = tmp_path / decision_path
    target.parent.mkdir(parents=True, exist_ok=True)
    publish_runtime_file(
        target,
        decision,
        state_root=tmp_path,
        create_only=True,
    )
    decision_sha256 = sha256_bytes(decision)
    owner = registry.acquire("queue-worker", scope=f"worker:{stage}")
    real_insert = queue._insert_semantic_decision

    def fail_index(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected semantic index failure")

    with queue.queue_owner(
        role="queue-worker", scope=f"worker:{stage}", parent=owner
    ):
        with memory_queue.capture_task_fences(
            queue,
            coordinator,
            binding.task_id,
            intent_id=intent_id,
            mode="worker",
            owner=owner,
        ) as (task_fence, intent_fence):
            assert intent_fence is not None
            monkeypatch.setattr(queue, "_insert_semantic_decision", fail_index)
            with pytest.raises(RuntimeError, match="injected semantic index failure"):
                queue.publish_semantic_decision(
                    coordinator,
                    task_id=binding.task_id,
                    intent_id=intent_id,
                    stage=stage,
                    decision_path=decision_path,
                    decision_sha256=decision_sha256,
                    active_link_digest=binding.active_digest,
                    task_fence=task_fence,
                    intent_fence=intent_fence,
                    owner=owner,
                )
            with sqlite3.connect(queue.db_path) as database:
                assert database.execute(
                    "SELECT COUNT(*) FROM capture_task_link_seals"
                ).fetchone() == (0,)
                assert database.execute(
                    "SELECT COUNT(*) FROM semantic_decisions"
                ).fetchone() == (0,)

            monkeypatch.setattr(queue, "_insert_semantic_decision", real_insert)
            indexed = queue.publish_semantic_decision(
                coordinator,
                task_id=binding.task_id,
                intent_id=intent_id,
                stage=stage,
                decision_path=decision_path,
                decision_sha256=decision_sha256,
                active_link_digest=binding.active_digest,
                task_fence=task_fence,
                intent_fence=intent_fence,
                owner=owner,
            )

    assert indexed.stage == stage
    assert indexed.seal_digest is not None
    with sqlite3.connect(queue.db_path) as database:
        task = database.execute(
            """SELECT state,result_reference,result_sha256,result_operation_id
               FROM tasks WHERE id=?""",
            (binding.task_id,),
        ).fetchone()
        rows = database.execute(
            "SELECT COUNT(*) FROM semantic_decisions WHERE intent_id=? AND stage=?",
            (intent_id, stage),
        ).fetchone()
        database.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", binding.task_id),
        )
    assert task == ("leased", None, None, None)
    assert rows == (1,)
    assert queue.recover_expired_leases() == 1
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state,result_reference FROM tasks WHERE id=?", (binding.task_id,)
        ).fetchone() == ("ready", None)
    assert target.read_bytes() == decision
    registry.release(owner)


def test_project_capture_binding_requires_live_intent_fence_and_is_immutable(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_id = "d" * 64
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id=intent_id,
        intent_sha256="e" * 64,
    )
    sealed = queue.seal_capture_binding(
        binding.task_id,
        consumer_kind="transaction",
        consumer_id="transaction:fixture",
        active_link_digest=binding.active_digest,
    )
    owner = registry.acquire("queue-worker", scope="worker:projection")
    fence = coordinator.acquire_intent_fence(intent_id, mode="worker", owner=owner)

    coordinator.project_capture_binding(sealed, intent_fence=fence)
    coordinator.project_capture_binding(sealed, intent_fence=fence)
    preconditions = {
        "intent_fence": {
            "intent_id": intent_id,
            "mode": "worker",
            "token": fence.token,
            "fencing_epoch": fence.epoch,
            "expires_at": fence.expires_at.isoformat().replace("+00:00", "Z"),
        },
        "capture_binding": {
            "intent_id": intent_id,
            "task_id": sealed.task_id,
            "active_link_digest": sealed.active_digest,
            "seal_digest": sealed.seal_digest,
        },
    }
    validated = coordinator._validate_preconditions(preconditions)
    with coordinator._connect() as database:
        coordinator._check_preconditions(validated, {}, database=database)
        assert tuple(database.execute(
            "SELECT COUNT(*) FROM capture_binding_projections"
        ).fetchone()) == (1,)

    coordinator.release_intent_fence(fence)
    with coordinator._connect() as database:
        with pytest.raises(markdown_transaction.TransactionFailure) as error:
            coordinator._check_preconditions(validated, {}, database=database)
    assert error.value.code == "precondition_failed"
    registry.release(owner)
