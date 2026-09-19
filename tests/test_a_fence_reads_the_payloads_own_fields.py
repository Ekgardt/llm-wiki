"""A source fence matches a payload's identity fields, not a date in its text.

`_payload_references_source` answered `daily_id in payload_json`. A daily id is a
date and every timestamp this product writes starts with one, so while
`archive_daily` held a fence over a day, every task carrying a timestamp of that
day was fenced, and the fence could not be taken while any such task was alive.

See `docs/research/
2026-09-18-a-source-is-referenced-by-a-field-not-by-a-date-in-the-text.md`.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue, QueueOperationError  # noqa: E402

_DAY = "2026-01-01"
_DIGEST = "c" * 64
# The date is in a field that names no source: this is a note written that day.
_UNRELATED = {"page": "knowledge/notes/a-page.md", "written_at": f"{_DAY}T09:00:00Z"}


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def queue(tmp_path: Path) -> MemoryQueue:
    return MemoryQueue(tmp_path, clock=_Clock(), rng=random.Random(7))


def _v3_queue(tmp_path: Path) -> MemoryQueue:
    candidate = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(candidate, source_v2=None)
    return MemoryQueue._from_v3_candidate(candidate, state_root=tmp_path)


def test_a_timestamp_of_the_fenced_day_is_not_a_reference(queue: MemoryQueue) -> None:
    fence = queue.acquire_source_fence(_DAY, _DIGEST)

    task_id = queue.enqueue("compile", 1, dict(_UNRELATED))

    queue.release_source_fence(fence.token)
    assert isinstance(task_id, str)


def test_the_fenced_source_itself_is_still_refused(queue: MemoryQueue) -> None:
    queue.acquire_source_fence(_DAY, _DIGEST)

    with pytest.raises(QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"daily_id": _DAY})
    with pytest.raises(QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"logical_path": f"knowledge/daily/{_DAY}.md"})


def test_a_task_that_only_mentions_the_day_does_not_block_the_fence(
    queue: MemoryQueue,
) -> None:
    queue.enqueue("compile", 1, dict(_UNRELATED))

    fence = queue.acquire_source_fence(_DAY, _DIGEST)

    assert fence.daily_id == _DAY


def test_the_adopted_queue_reads_the_same_fields(tmp_path: Path) -> None:
    queue = _v3_queue(tmp_path)
    fence = queue.acquire_source_fence(_DAY, _DIGEST)

    task_id = queue.enqueue("compile", 1, dict(_UNRELATED))
    with pytest.raises(QueueOperationError, match="source_fenced"):
        queue.enqueue("compile", 1, {"source_digest": _DIGEST})

    queue.release_source_fence(fence.token)
    assert isinstance(task_id, str)


def test_a_cancelled_lease_is_written_into_the_attempt_history(
    tmp_path: Path,
) -> None:
    """The adopted queue records a cancelled attempt, as the legacy queue does."""
    queue = _v3_queue(tmp_path)
    task_id = queue.enqueue("compile", 1, {"daily_id": _DAY})
    queue.claim("worker")

    cancelled = queue.cancel(task_id)

    with sqlite3.connect(queue.db_path) as connection:
        rows = connection.execute(
            "SELECT outcome, error_code FROM attempt_history WHERE task_id=?",
            (task_id,),
        ).fetchall()
    assert (cancelled, rows) == (True, [("cancelled", "cancelled")])
