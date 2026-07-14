"""Operator CLI and bounded-worker tests for memory_queue."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from memory_queue import MemoryQueue  # noqa: E402


def test_worker_defaults_are_bounded_and_process_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    for number in range(25):
        queue.enqueue("query", 1, {"number": number})
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)

    summary = memory_queue.run_worker(lambda task: True)

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

    summary = memory_queue.run_worker(lambda task: True, monotonic=lambda: next(times))

    assert summary.processed == 0
    assert queue.list_tasks(states=("ready",))[0].state == "ready"


def test_worker_stops_after_two_idle_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    monkeypatch.setattr(memory_queue, "_queue", lambda: queue)
    sleeps: list[float] = []

    summary = memory_queue.run_worker(
        lambda task: True,
        sleep=lambda seconds: sleeps.append(seconds),
        monotonic=iter((0.0, 0.0, 1.0, 2.0)).__next__,
    )

    assert summary.processed == 0
    assert sum(sleeps) == 2


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
