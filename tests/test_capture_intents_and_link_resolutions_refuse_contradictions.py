from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import markdown_transaction
import pytest
import queue_core
from llm_wiki.queue_contracts import CaptureTaskBinding, QueueOperationError
from markdown_transaction import MarkdownCoordinator
from operational_ownership import OwnerLease, OwnershipRegistry
from queue_core import QueueV3CandidateReader

INTENT = "7" * 64
INTENT_SHA = "8" * 64
OTHER_INTENT = "9" * 64
PENDING = f"run/capture-intents/pending/{INTENT[:2]}/{INTENT}.json"
READY = f"run/capture-intents/ready/{INTENT[:2]}/{INTENT}.json"


@pytest.fixture
def queue(tmp_path: Path) -> QueueV3CandidateReader:
    coordinator = tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run/queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(coordinator, source_v2=None)
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)
    return queue_core.MemoryQueue.from_v3_candidate(candidate, state_root=tmp_path)


@pytest.fixture
def registry(tmp_path: Path) -> OwnershipRegistry:
    return OwnershipRegistry(tmp_path)


@pytest.fixture
def repair(registry: OwnershipRegistry) -> Iterator[OwnerLease]:
    owner = registry.acquire("repair", scope="repair:links", actor_id="repairer")
    yield owner
    registry.release(owner)


def _publish(queue: QueueV3CandidateReader, intent_id: str, intent_sha256: str) -> str:
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id, intent_path=intent_path, intent_sha256=intent_sha256, byte_size=128
    )
    return intent_path


@pytest.fixture
def binding(
    queue: QueueV3CandidateReader, registry: OwnershipRegistry, tmp_path: Path
) -> CaptureTaskBinding:
    coordinator = MarkdownCoordinator.from_v3_candidate(
        tmp_path / "run/markdown-transactions-v3.candidate.sqlite3", state_root=tmp_path
    )
    intent_path = _publish(queue, INTENT, INTENT_SHA)
    owner = registry.acquire("capture", scope=f"intent:{INTENT}")
    fence = coordinator.acquire_intent_fence(INTENT, mode="capture", owner=owner)
    created = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": "capture"},
        intent_id=INTENT,
        intent_path=intent_path,
        intent_sha256=INTENT_SHA,
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return created


@pytest.mark.parametrize("limit", [0, True, cast(int, "5")])
def test_a_pending_intent_scan_needs_a_positive_integer_limit(
    queue: QueueV3CandidateReader, limit: int
) -> None:
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        queue.pending_capture_intents(limit)


def test_republishing_the_same_intent_is_idempotent_and_a_different_one_conflicts(
    queue: QueueV3CandidateReader,
) -> None:
    _publish(queue, INTENT, INTENT_SHA)
    _publish(queue, INTENT, INTENT_SHA)

    with pytest.raises(QueueOperationError, match="capture_intent_conflict"):
        _publish(queue, INTENT, "f" * 64)


def _pending(queue: QueueV3CandidateReader, byte_size: int = 128) -> str:
    return queue.index_capture_intent_pending(
        intent_id=INTENT,
        pending_path=PENDING,
        ready_path=READY,
        intent_sha256=INTENT_SHA,
        byte_size=byte_size,
    )


def _ready(queue: QueueV3CandidateReader) -> None:
    queue.mark_capture_intent_ready(
        intent_id=INTENT,
        pending_path=PENDING,
        ready_path=READY,
        intent_sha256=INTENT_SHA,
        byte_size=128,
    )


def test_a_pending_intent_is_indexed_once_and_promoted_once(
    queue: QueueV3CandidateReader,
) -> None:
    assert _pending(queue) == "pending"
    assert _pending(queue) == "pending"

    _ready(queue)
    _ready(queue)

    assert _pending(queue) == "ready"
    assert queue.pending_capture_intents(10) == []


def test_reindexing_an_intent_with_another_size_conflicts(queue: QueueV3CandidateReader) -> None:
    _pending(queue)

    with pytest.raises(QueueOperationError, match="capture_intent_conflict"):
        _pending(queue, byte_size=129)


def test_promoting_an_intent_never_indexed_conflicts(queue: QueueV3CandidateReader) -> None:
    with pytest.raises(QueueOperationError, match="capture_intent_conflict"):
        _ready(queue)


def test_only_a_capture_owner_may_enqueue_a_capture_task(
    queue: QueueV3CandidateReader, repair: OwnerLease
) -> None:
    with pytest.raises(ValueError, match="requires a capture owner"):
        queue.enqueue_capture_task(
            "flush",
            1,
            {"prompt": "capture"},
            intent_id=INTENT,
            intent_path=f"run/capture-intents/{INTENT}.json",
            intent_sha256=INTENT_SHA,
            capture_fence=None,
            owner=repair,
        )


def _resolve(
    queue: QueueV3CandidateReader,
    binding: CaptureTaskBinding,
    owner: OwnerLease,
    supersedes: str | None,
    selected: dict[str, object] | None = None,
) -> CaptureTaskBinding:
    return queue.append_capture_link_resolution(
        binding.task_id,
        supersedes_digest=supersedes,
        observed={
            "link_digest": binding.active_digest,
            "index_digest": binding.active_digest,
            "intent_file_digest": INTENT_SHA,
        },
        selected_intent=selected,
        owner=owner,
        reason="Operator resolved the link.",
    )


def test_only_a_repair_owner_may_resolve_a_capture_link(
    queue: QueueV3CandidateReader, binding: CaptureTaskBinding, registry: OwnershipRegistry
) -> None:
    owner = registry.acquire("capture", scope="intent:other")
    try:
        with pytest.raises(ValueError, match="requires a repair owner"):
            _resolve(queue, binding, owner, binding.active_digest)
    finally:
        registry.release(owner)


def _operator(queue: QueueV3CandidateReader, repair: OwnerLease) -> Any:
    return queue.queue_owner(role="queue-operator", scope="task:links", parent=repair)


def test_a_resolution_that_supersedes_a_stale_digest_conflicts(
    queue: QueueV3CandidateReader, binding: CaptureTaskBinding, repair: OwnerLease
) -> None:
    with (
        _operator(queue, repair),
        pytest.raises(QueueOperationError, match="capture_link_conflicted"),
    ):
        _resolve(queue, binding, repair, "0" * 64)


def test_a_resolution_selecting_no_intent_unbinds_the_task(
    queue: QueueV3CandidateReader, binding: CaptureTaskBinding, repair: OwnerLease
) -> None:
    with _operator(queue, repair):
        resolved = _resolve(queue, binding, repair, binding.active_digest)

    assert resolved.intent_id is None
    assert resolved.active_digest != binding.active_digest


def test_a_resolution_selecting_a_known_intent_rebinds_the_task(
    queue: QueueV3CandidateReader, binding: CaptureTaskBinding, repair: OwnerLease
) -> None:
    _publish(queue, OTHER_INTENT, INTENT_SHA)
    selected: dict[str, object] = {
        "intent_id": OTHER_INTENT,
        "intent_sha256": INTENT_SHA,
        "handler_version": 1,
    }

    with _operator(queue, repair):
        resolved = _resolve(queue, binding, repair, binding.active_digest, selected)

    assert resolved.intent_id == OTHER_INTENT


def test_a_resolution_selecting_an_intent_by_the_wrong_digest_conflicts(
    queue: QueueV3CandidateReader, binding: CaptureTaskBinding, repair: OwnerLease
) -> None:
    selected: dict[str, object] = {
        "intent_id": INTENT,
        "intent_sha256": "f" * 64,
        "handler_version": 1,
    }

    with (
        _operator(queue, repair),
        pytest.raises(QueueOperationError, match="capture_link_conflicted"),
    ):
        _resolve(queue, binding, repair, binding.active_digest, selected)


def test_redriving_a_task_that_does_not_exist_names_it(queue: QueueV3CandidateReader) -> None:
    with pytest.raises(KeyError, match="no-such-task"):
        queue.redrive("no-such-task")
