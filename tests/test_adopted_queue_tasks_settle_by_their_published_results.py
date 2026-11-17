from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import markdown_transaction
import pytest
import queue_core
from llm_wiki import queue_reader_tasks
from queue_core import QueueFailure, QueueLease, QueueV3CandidateReader


@pytest.fixture
def queue(tmp_path: Path) -> QueueV3CandidateReader:
    coordinator = tmp_path / "run/markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run/queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(coordinator, source_v2=None)
    queue_core.initialize_queue_v3_candidate(candidate, source_v2=None)
    return queue_core.MemoryQueue.from_v3_candidate(candidate, state_root=tmp_path)


def _leased(queue: QueueV3CandidateReader) -> QueueLease:
    queue.enqueue("query", 1, {"query": "requested"})
    lease = queue.claim("worker")
    assert lease is not None
    return lease


def test_reading_a_task_that_does_not_exist_names_it(queue: QueueV3CandidateReader) -> None:
    with pytest.raises(KeyError, match="no-such-task"):
        queue.get("no-such-task")


def test_cancelling_a_missing_or_finished_task_changes_nothing(
    queue: QueueV3CandidateReader,
) -> None:
    task_id = queue.enqueue("query", 1, {"query": "requested"})
    assert queue.cancel(task_id) is True

    assert queue.cancel(task_id) is False
    assert queue.cancel("no-such-task") is False
    assert queue.get(task_id).state == "cancelled"


def test_a_failure_without_an_error_code_is_refused(queue: QueueV3CandidateReader) -> None:
    lease = _leased(queue)

    with pytest.raises(ValueError, match="failure must have a non-empty error code"):
        queue.fail(lease, QueueFailure(""))

    assert queue.get(lease.id).state == "leased"


def test_an_acknowledged_lease_without_a_result_is_buried_as_corrupt(
    queue: QueueV3CandidateReader,
) -> None:
    lease = _leased(queue)

    task = queue.acknowledge(lease)

    assert (task.state, task.error_code) == ("dead", "result_corrupt")


def test_an_acknowledged_lease_with_its_result_succeeds(queue: QueueV3CandidateReader) -> None:
    lease = _leased(queue)
    queue.publish_result(lease, operation_id="op-1", result=b"answer")

    task = queue.acknowledge(lease)

    assert (task.state, task.error_code) == ("succeeded", None)


def test_adopting_needs_an_operation_id(queue: QueueV3CandidateReader) -> None:
    lease = _leased(queue)

    with pytest.raises(ValueError, match="operation_id must be non-empty"):
        queue.adopt_published_result(lease, operation_id="")


def test_adopting_a_result_nobody_published_answers_nothing(
    queue: QueueV3CandidateReader,
) -> None:
    lease = _leased(queue)

    assert queue.adopt_published_result(lease, operation_id="never-published") is None
    assert queue.get(lease.id).state == "leased"


def test_a_published_result_is_adopted_and_bound_to_the_lease(
    queue: QueueV3CandidateReader,
) -> None:
    lease = _leased(queue)
    relative = queue.publish_result(lease, operation_id="op-1", result=b"answer")

    assert queue.adopt_published_result(lease, operation_id="op-1") == "adopted"

    task = queue.get(lease.id)
    assert (task.state, task.result_reference) == ("leased", relative)
    assert queue.acknowledge(lease).state == "succeeded"


def _vanished(path: Path, max_bytes: int) -> bytes:
    raise FileNotFoundError(path)


def test_a_published_result_that_vanishes_before_it_is_read_buries_the_task(
    queue: QueueV3CandidateReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _leased(queue)
    queue.publish_result(lease, operation_id="op-1", result=b"answer")
    monkeypatch.setattr(queue_reader_tasks, "read_stable_owner_file", _vanished)

    assert queue.adopt_published_result(lease, operation_id="op-1") == "corrupt"

    task = queue.get(lease.id)
    assert (task.state, task.error_code) == ("dead", "result_corrupt")


def test_retained_results_alone_hold_the_run_directory(
    queue: QueueV3CandidateReader, tmp_path: Path
) -> None:
    assert queue.retains_run_directory() is False
    results = tmp_path / "run/queue-results"
    results.mkdir(exist_ok=True)
    (results / "kept.result").write_bytes(b"answer")

    assert queue.retains_run_directory() is True


def test_a_restore_requeues_what_a_purge_of_the_adopted_queue_exported(
    queue: QueueV3CandidateReader, tmp_path: Path
) -> None:
    exports = tmp_path / "exports"
    exports.mkdir(mode=0o700)
    task_id = queue.enqueue("query", 1, {"query": "requested"})
    queue.cancel(task_id)
    purged = queue.purge(
        terminal_before=datetime.now(UTC) + timedelta(days=400),
        export_path=exports / "one",
    )

    restored = queue.restore(export_path=exports / "one")

    assert restored.restored == purged.purged
