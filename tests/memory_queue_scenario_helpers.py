"""The clock, queue fixtures and helpers the memory queue scenario files share."""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import queue_core  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from queue_core import (  # noqa: E402
    MemoryQueue,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(tmp_path: Path, clock: FakeClock) -> MemoryQueue:
    return MemoryQueue(tmp_path, clock=clock, rng=random.Random(7))


def _counts(summary) -> tuple[int, int, int, int]:
    """One worker run's outcome as (succeeded, failed, dead, skipped)."""
    return (summary.succeeded, summary.failed, summary.dead, summary.skipped)


def _worked(processor, *, max_tasks: int = 1):
    """One bounded worker pass, running the handler in this process."""
    return queue_core.run_worker(
        processor,
        max_tasks=max_tasks,
        idle_seconds=0,
        processor_runner=queue_core.run_processor_inline,
    )


def _task_facts(task) -> tuple:
    """A task's state, error code, and the outcome and code of its last attempt."""
    last = task.attempt_history[-1] if task.attempt_history else None
    return (
        task.state,
        task.error_code,
        None if last is None else last.outcome,
        None if last is None else last.error_code,
    )
