"""A dead capture gets one redrive after the code changes, and the purge keeps what it cannot prove.

Twenty-five capture tasks died before the causes were fixed and nothing moved
them; the weekly purge aborted on the first one without a terminal record. See
docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
import scheduled_nightly  # noqa: E402

LONG_AGO = datetime.now(timezone.utc) - timedelta(days=60)


def _queue(tmp_path: Path):
    coordinator = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(coordinator, source_v2=None)
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)


def _dead_capture(tmp_path: Path, queue, intent_id: str) -> str:
    """A capture task that spent its attempts long ago, as the live ones did."""
    coordinator = markdown_transaction.MarkdownCoordinator._from_v3_candidate(
        tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3", state_root=tmp_path
    )
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_path = f"run/capture-intents/{intent_id}.json"
    queue.publish_capture_intent(
        intent_id=intent_id, intent_path=intent_path, intent_sha256="2" * 64, byte_size=128
    )
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task(
        "flush",
        1,
        {"prompt": intent_id},
        intent_id=intent_id,
        intent_path=intent_path,
        intent_sha256="2" * 64,
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    _age(queue, binding.task_id, "dead", "processor_failed")
    return binding.task_id


def _age(queue, task_id: str, state: str, error_code: str | None) -> None:
    stamp = memory_queue._timestamp(LONG_AGO)
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET state=?, error_code=?, attempts=8, updated_at=? WHERE id=?",
            (state, error_code, stamp, task_id),
        )


def _children(queue, task_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(queue.db_path) as database:
        return database.execute(
            "SELECT t.state, l.intent_id FROM tasks AS t "
            "LEFT JOIN capture_task_links AS l ON l.task_id=t.id WHERE t.redrive_of=?",
            (task_id,),
        ).fetchall()


def test_a_capture_that_died_before_the_change_is_redriven_once_with_its_intent(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    dead = _dead_capture(tmp_path, queue, "a" * 64)
    now = datetime.now(timezone.utc)

    first = queue.redrive_dead_captures(changed_after=now)
    second = queue.redrive_dead_captures(changed_after=now)

    assert ([pair[0] for pair in first.redriven], second.redriven) == ([dead], ())
    assert _children(queue, dead) == [("ready", "a" * 64)]


def test_a_capture_that_died_after_the_change_waits_for_the_next_one(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _dead_capture(tmp_path, queue, "b" * 64)

    outcome = queue.redrive_dead_captures(changed_after=LONG_AGO - timedelta(days=1))

    assert (outcome.redriven, outcome.refused) == ((), ())


def _unreachable_child(queue, dead: str) -> str:
    """A redrive as it was made before 2026-09-07: a copy without the capture link."""
    child = queue.redrive(dead)
    with sqlite3.connect(queue.db_path) as database:
        database.execute("DROP TRIGGER IF EXISTS capture_task_links_authorized_delete")
        database.execute("DELETE FROM capture_task_links WHERE task_id=?", (child,))
    queue.cancel(child)
    return child


def test_a_child_that_could_not_reach_the_worker_does_not_spend_the_second_chance(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    dead = _dead_capture(tmp_path, queue, "c" * 64)
    _unreachable_child(queue, dead)

    outcome = queue.redrive_dead_captures(changed_after=datetime.now(timezone.utc))

    assert [pair[0] for pair in outcome.redriven] == [dead]


def test_a_child_that_carried_the_link_spent_it_even_when_cancelled(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    dead = _dead_capture(tmp_path, queue, "e" * 64)
    queue.cancel(queue.redrive(dead))

    outcome = queue.redrive_dead_captures(changed_after=datetime.now(timezone.utc))

    assert (outcome.redriven, outcome.refused) == ((), ())


def test_the_purge_keeps_a_capture_it_cannot_prove_and_takes_the_rest(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    dead = _dead_capture(tmp_path, queue, "d" * 64)
    finished = queue.enqueue("query", 1, {"prompt": "finished"})
    lease = queue.claim("worker")
    queue.publish_result(lease, operation_id="op", result=b"answer")
    queue.acknowledge(lease)
    _age(queue, finished, "succeeded", None)

    receipt = queue.purge(
        terminal_before=datetime.now(timezone.utc),
        export_path=tmp_path / "export",
        include_dead=True,
    )

    assert (receipt.task_ids, receipt.retained) == ((finished,), (dead,))


def test_the_nightly_redrives_before_the_queue_worker_runs() -> None:
    labels = [step.label for step in scheduled_nightly._intake_steps()]

    assert labels.index("dead_capture_redrive") < labels.index("work")


def test_no_readable_head_means_no_redrive_step(monkeypatch) -> None:
    monkeypatch.setattr(scheduled_nightly, "head_commit_time", lambda: None)

    assert "dead_capture_redrive" not in [step.label for step in scheduled_nightly._intake_steps()]
