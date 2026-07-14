"""Operator CLI and bounded-worker tests for memory_queue."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
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


def _grandchild_processor(task: dict) -> bool:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(task["payload"]["pid_path"]).write_text(str(child.pid), encoding="ascii")
    time.sleep(30)
    return True


class _MaxJitterRng:
    def getrandbits(self, bits: int) -> int:
        return 1 << (bits - 1)

    def uniform(self, low: float, high: float) -> float:
        del low
        return high


class _FakeProcess:
    def __init__(self, *, stubborn: bool = False) -> None:
        self.pid = 4242
        self.alive = True
        self.stubborn = stubborn
        self.terminated = 0
        self.killed = 0

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        del timeout

    def terminate(self) -> None:
        self.terminated += 1
        if not self.stubborn:
            self.alive = False

    def kill(self) -> None:
        self.killed += 1
        if not self.stubborn:
            self.alive = False


def test_worker_defaults_are_bounded_and_process_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    for number in range(25):
        queue.enqueue("query", 1, {"number": number})
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)

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
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
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
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
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
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
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


def test_worker_policy_overrides_claim_heartbeat_attempts_and_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    intervals: list[float] = []

    def heartbeat_wait(stop, interval: float) -> bool:
        intervals.append(interval)
        return stop.wait(1)

    queue = MemoryQueue(
        tmp_path,
        clock=lambda: now,
        heartbeat_wait=heartbeat_wait,
    )
    task_id = queue.enqueue("query", 1, {})
    with queue._connect() as connection:
        connection.execute("UPDATE tasks SET attempts=8 WHERE id=?", (task_id,))
    queue._rng = _MaxJitterRng()
    claimed_leases: list[int] = []
    real_claim = queue.claim

    def claim(owner: str, **kwargs):
        claimed_leases.append(kwargs["lease_seconds"])
        return real_claim(owner, **kwargs)

    monkeypatch.setattr(queue, "claim", claim)
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)

    summary = memory_queue.run_worker(
        lambda task: False,
        max_tasks=1,
        idle_seconds=0,
        lease_seconds=10,
        heartbeat_seconds=3,
        max_attempts=10,
        retry_base_seconds=5,
        retry_cap_seconds=7,
        processor_runner=memory_queue._run_processor_inline,
    )

    task = queue.get(task_id)
    assert summary.failed == 1
    assert claimed_leases == [10]
    assert intervals == [3]
    assert task.state == "ready"
    assert task.attempts == 9
    assert (task.available_at - now).total_seconds() == 7


def test_worker_child_process_is_terminated_at_deadline() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        memory_queue._run_processor_child(
            _sleep_processor,
            {"payload": {"seconds": 5}},
            0.2,
        )

    assert time.monotonic() - started < 2


def test_worker_timeout_kills_spawned_grandchild_tree(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    with pytest.raises(TimeoutError):
        memory_queue._run_processor_child(
            _grandchild_processor,
            {"payload": {"pid_path": str(pid_path)}},
            1,
        )
    assert pid_path.exists()
    pid = int(pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5
    while memory_queue._pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not memory_queue._pid_is_alive(pid)
    finally:
        if memory_queue._pid_is_alive(pid):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            else:
                os.kill(pid, signal.SIGKILL)


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
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
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
    export_parent = tmp_path / "exports"
    export_parent.mkdir()
    memory_queue._harden_owner_only(export_parent, 0o700)
    export = export_parent / "export"
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
        "lease_seconds": 120,
        "heartbeat_seconds": 40,
        "max_attempts": 8,
        "retry_base_seconds": 30,
        "retry_cap_seconds": 3600,
    }
    assert set(json.loads(capsys.readouterr().out)) == {"counts"}


def test_cli_work_forwards_policy_overrides(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def worker(processor, **kwargs):
        seen.update(kwargs)
        return memory_queue.WorkerSummary(0, 0, 0, 0, 0)

    monkeypatch.setattr(memory_queue, "run_worker", worker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_queue.py",
            "work",
            "--lease-seconds",
            "30",
            "--heartbeat-seconds",
            "10",
            "--max-attempts",
            "12",
            "--retry-base-seconds",
            "4",
            "--retry-cap-seconds",
            "40",
        ],
    )

    assert memory_queue._cli() == 0
    assert seen["lease_seconds"] == 30
    assert seen["heartbeat_seconds"] == 10
    assert seen["max_attempts"] == 12
    assert seen["retry_base_seconds"] == 4
    assert seen["retry_cap_seconds"] == 40
    assert set(json.loads(capsys.readouterr().out)) == {"counts"}


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"lease_seconds": 0}, "lease_seconds"),
        ({"heartbeat_seconds": 0}, "heartbeat_seconds"),
        ({"lease_seconds": 10, "heartbeat_seconds": 10}, "heartbeat"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": 101}, "max_attempts"),
        ({"retry_base_seconds": 0}, "retry_base_seconds"),
        ({"retry_cap_seconds": 0}, "retry_cap_seconds"),
        ({"retry_base_seconds": 11, "retry_cap_seconds": 10}, "retry_base"),
    ],
)
def test_worker_rejects_invalid_policy_combinations(overrides, match) -> None:
    with pytest.raises(ValueError, match=match):
        memory_queue.run_worker(
            lambda task: True,
            processor_runner=memory_queue._run_processor_inline,
            **overrides,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["memory_queue.py", "drain", "C:/secret/arbitrary-path"],
        ["memory_queue.py", "work", "--max-tasks", "secret-not-an-int"],
        ["memory_queue.py"],
    ],
)
def test_cli_parse_failures_are_redacted_canonical_invalid_arguments(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    assert memory_queue._cli() == 2
    captured = capsys.readouterr()
    assert captured.out == '{"code":"invalid_arguments","ok":false}\n'
    assert captured.err == ""
    assert "secret" not in captured.out


def test_windows_tree_cleanup_failure_falls_back_and_reports_unverified_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(
        memory_queue.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1),
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._terminate_processor_child(process, platform_name="nt")

    assert raised.value.code == "process_cleanup_failed"
    assert process.terminated == 1
    assert not process.is_alive()


def test_windows_tree_cleanup_verifies_direct_child_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stubborn=True)
    monkeypatch.setattr(
        memory_queue.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._terminate_processor_child(process, platform_name="nt")

    assert raised.value.code == "process_cleanup_failed"
    assert process.terminated == 1
    assert process.killed == 1


def test_posix_group_race_uses_direct_fallback_and_reports_unverified_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(
        memory_queue,
        "_kill_process_group",
        lambda *args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._terminate_processor_child(process, platform_name="posix")

    assert raised.value.code == "process_cleanup_failed"
    assert process.terminated == 1
    assert not process.is_alive()


def test_posix_group_cleanup_waits_and_verifies_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()

    def kill_group(pid: int, sig: int) -> None:
        assert pid == process.pid
        if sig == signal.SIGTERM:
            process.alive = False

    monkeypatch.setattr(memory_queue, "_kill_process_group", kill_group)

    memory_queue._terminate_processor_child(process, platform_name="posix")

    assert not process.is_alive()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PermissionError("C:/secret/path"), "permission_denied"),
        (OSError("C:/secret/path"), "os_error"),
        (sqlite3.DatabaseError("C:/secret/db"), "sqlite_error"),
        (UnicodeDecodeError("utf-8", b"x", 0, 1, "secret"), "decode_error"),
        (subprocess.SubprocessError("secret command"), "process_error"),
    ],
)
def test_cli_expected_failures_emit_only_bounded_canonical_code(
    error: Exception,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "status"])
    monkeypatch.setattr(
        memory_queue, "_operator_status", lambda: (_ for _ in ()).throw(error)
    )

    assert memory_queue._cli() == 2
    captured = capsys.readouterr()
    assert captured.out == f'{{"codes":["{code}"]}}\n'
    assert captured.err == ""
    assert len(captured.out) < 128
    assert "secret" not in captured.out


def test_cli_unexpected_failure_emits_generic_internal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "status"])
    monkeypatch.setattr(
        memory_queue,
        "_operator_status",
        lambda: (_ for _ in ()).throw(RuntimeError("C:/secret/payload")),
    )

    assert memory_queue._cli() == 2
    captured = capsys.readouterr()
    assert captured.out == '{"codes":["internal_error"]}\n'
    assert captured.err == ""
