"""Operator CLI and bounded-worker tests for memory_queue."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue  # noqa: E402


def _sleep_processor(task: dict) -> bool:
    time.sleep(task["payload"]["seconds"])
    return True


def _result_processor(task: dict) -> memory_queue.DeferredResult:
    return memory_queue.DeferredResult(b"x" * task["payload"]["size"])


def test_worker_defaults_are_bounded_and_process_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    for number in range(25):
        queue.enqueue("query", 1, {"number": number})
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)

    summary = memory_queue.run_worker(
        lambda task: True, processor_runner=memory_queue._run_processor_inline
    )

    assert summary.processed == 20
    assert summary.succeeded == 20
    assert len(queue.list_tasks(states=("ready",))) == 5


def test_worker_stops_at_elapsed_limit_without_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    queue.enqueue("query", 1, {})
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    times = iter((0.0, 601.0))

    summary = memory_queue.run_worker(
        lambda task: True,
        monotonic=lambda: next(times),
        processor_runner=memory_queue._run_processor_inline,
    )

    assert summary.processed == 0
    assert queue.list_tasks(states=("ready",))[0].state == "ready"


def test_worker_stops_after_two_idle_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    summary = memory_queue.run_worker(
        lambda task: True,
        sleep=sleep,
        monotonic=lambda: now[0],
    )

    assert summary.processed == 0
    assert sleeps == [1.0, 1.0]


def test_worker_times_out_handler_without_result_or_live_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("query", 1, {})
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    now = [0.0]

    def runner(processor, task, timeout):
        del processor, task
        now[0] += timeout
        return memory_queue.DeferredResult(b"late-result")

    summary = memory_queue.run_worker(
        lambda task: True,
        max_seconds=3,
        idle_seconds=0,
        monotonic=lambda: now[0],
        processor_runner=runner,
    )

    task = queue.get(task_id)
    assert summary.failed == 1
    assert task.state == "ready"
    assert task.error_code == "worker_timeout"
    assert task.lease_token is None
    assert task.result_reference is None
    assert now[0] == 3


def test_worker_child_process_is_terminated_at_deadline() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        memory_queue._run_processor_child(
            _sleep_processor,
            {"payload": {"seconds": 5}},
            0.2,
        )

    assert time.monotonic() - started < 2


def test_worker_child_drains_one_megabyte_result_before_join() -> None:
    result = memory_queue._run_processor_child(
        _result_processor,
        {"payload": {"size": 1024 * 1024}},
        10,
    )

    assert isinstance(result, memory_queue.DeferredResult)
    assert len(result.data) == 1024 * 1024


def test_worker_child_drains_result_at_queue_size_limit() -> None:
    result = memory_queue._run_processor_child(
        _result_processor,
        {"payload": {"size": memory_queue._MAX_RESULT_BYTES}},
        10,
    )

    assert isinstance(result, memory_queue.DeferredResult)
    assert len(result.data) == memory_queue._MAX_RESULT_BYTES


def test_worker_child_rejects_oversize_result_without_deadlock() -> None:
    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._run_processor_child(
            _result_processor,
            {"payload": {"size": memory_queue._MAX_RESULT_BYTES + 1}},
            10,
        )

    assert raised.value.code == "processor_result_oversize"


@pytest.mark.parametrize("frame", [b"", b"unknown", b"Ttrailing", b"Eextra"])
def test_worker_child_rejects_malformed_ipc_frame(frame: bytes) -> None:
    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._decode_processor_frame(frame)

    assert raised.value.code == "processor_result_malformed"


def test_idle_sleep_is_capped_by_worker_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    memory_queue.run_worker(
        lambda task: True,
        max_seconds=0.25,
        idle_seconds=2,
        monotonic=lambda: now[0],
        sleep=sleep,
        processor_runner=memory_queue._run_processor_inline,
    )

    assert sleeps == [0.25]


def test_cli_list_outputs_only_operator_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    memory_queue.migrate_legacy_queue(tmp_path)
    task_id = memory_queue.enqueue("query", {"prompt": "do not print"})
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "list"])

    assert memory_queue._cli() == 0

    output = capsys.readouterr().out
    record = json.loads(output)
    assert record == [{"id": task_id, "state": "ready"}]
    assert "prompt" not in output and str(tmp_path) not in output


def test_cli_status_outputs_counts_states_capabilities_and_codes_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    memory_queue.migrate_legacy_queue(tmp_path)
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "status"])

    assert memory_queue._cli() == 0

    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"counts", "states", "capabilities", "codes"}
    assert str(tmp_path) not in json.dumps(output)


def test_cli_cancel_redrive_migrate_and_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "migrate"])
    assert memory_queue._cli() == 0
    assert set(json.loads(capsys.readouterr().out)) == {"counts", "codes"}

    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("query", 1, {})
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "cancel", task_id])
    assert memory_queue._cli() == 0
    assert json.loads(capsys.readouterr().out) == {"id": task_id, "state": "cancelled"}

    old = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None and lease.id == old
    queue.fail(lease, memory_queue.QueueFailure("invalid_input", permanent=True))
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "redrive", old])
    assert memory_queue._cli() == 0
    redriven = json.loads(capsys.readouterr().out)
    assert set(redriven) == {"id", "state"}
    assert redriven["state"] == "ready"

    before = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    export = tmp_path / "export"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_queue.py",
            "purge",
            "--terminal-before",
            before,
            "--export",
            str(export),
        ],
    )
    assert memory_queue._cli() == 0
    assert set(json.loads(capsys.readouterr().out)) == {"counts", "ids"}


def test_cli_work_uses_operational_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def worker(processor, **kwargs):
        seen.update(kwargs)
        return memory_queue.WorkerSummary(0, 0, 0, 0, 0)

    monkeypatch.setattr(memory_queue, "run_worker", worker)
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "work"])

    assert memory_queue._cli() == 0
    assert seen == {
        "max_tasks": 20,
        "max_seconds": 600,
        "idle_seconds": 2,
    }
    assert set(json.loads(capsys.readouterr().out)) == {"counts"}


def test_cli_rejects_removed_drain_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "drain"])

    with pytest.raises(SystemExit) as raised:
        memory_queue._cli()

    assert raised.value.code == 2
