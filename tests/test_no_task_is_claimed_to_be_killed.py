"""A worker claims no task it has too little time left to finish.

`doctor --repair` ran a one-second worker that claimed a model task and killed it, one
attempt per repair; every worker did the same in its last seconds. Research:
`docs/research/2026-09-14-no-task-is-claimed-to-be-killed.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue  # noqa: E402


def _worker_with_seconds_left(tmp_path: Path, monkeypatch, seconds: float):
    queue = MemoryQueue(tmp_path)
    queue.enqueue("query", 1, {})
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
    summary = memory_queue.run_worker(
        lambda task: True,
        max_tasks=1,
        max_seconds=int(seconds),
        monotonic=lambda: 0.0,
        processor_runner=memory_queue._run_processor_inline,
        min_claim_seconds=memory_queue.WORKER_MIN_CLAIM_SECONDS,
    )
    return summary, queue


def test_too_little_time_left_claims_nothing_and_spends_no_attempt(tmp_path, monkeypatch):
    summary, queue = _worker_with_seconds_left(tmp_path, monkeypatch, memory_queue.WORKER_MIN_CLAIM_SECONDS - 1)
    task = queue.list_tasks(states=("ready",))[0]

    assert (summary.processed, task.attempts) == (0, 0)


def test_enough_time_left_still_works(tmp_path, monkeypatch):
    summary, _queue = _worker_with_seconds_left(tmp_path, monkeypatch, memory_queue.WORKER_MIN_CLAIM_SECONDS + 1)

    assert summary.succeeded == 1


def test_the_work_command_keeps_the_margin_by_default():
    arguments = memory_queue._build_cli_parser().parse_args(["work"])

    assert arguments.min_claim_seconds == memory_queue.WORKER_MIN_CLAIM_SECONDS
