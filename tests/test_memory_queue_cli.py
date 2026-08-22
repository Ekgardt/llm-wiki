"""Operator CLI and bounded-worker tests for memory_queue."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import nullcontext
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


def _report_alive(sender) -> None:
    """Report and stay alive; the parent decides when this child ends."""
    sender.send_bytes(b"R")
    time.sleep(120)


def test_a_spawned_child_of_this_module_starts_and_reports_back() -> None:
    """Windows hides a spawn child's bootstrap failure: it dies with no output.

    Several worker tests failed there with `child exited 1 before cleanup`,
    which is what the parent sees when the child never ran. This isolates that
    question from the worker logic around it.
    """
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    process = context.Process(target=_report_alive, args=(sender,), daemon=False)
    process.start()
    sender.close()
    try:
        reported = receiver.poll(60)
        assert reported, f"child exited {process.exitcode} without reporting"
        assert receiver.recv_bytes(1) == b"R"
    finally:
        process.terminate()
        process.join(30)
        receiver.close()


def _silent_sleeper() -> None:
    """Sleep without reporting: the shape the worker's own child has."""
    time.sleep(120)


def test_a_spawned_child_that_only_sleeps_is_still_alive_five_seconds_later() -> None:
    """The worker's child dies with exit code 1 on Windows before its deadline.

    Its sibling probe, which reports first and then sleeps, survives — so this
    one isolates the case where the child produces nothing before it is killed.
    """
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_silent_sleeper, daemon=False)
    process.start()
    try:
        time.sleep(5)
        alive = process.is_alive()
        exitcode = process.exitcode
        assert alive, f"sleeping child exited {exitcode} on its own"
    finally:
        process.terminate()
        process.join(30)


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


def _exiting_grandchild_processor(task: dict) -> bool:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(task["payload"]["pid_path"]).write_text(str(child.pid), encoding="ascii")
    return True


def _malformed_grandchild_processor(task: dict) -> str:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(task["payload"]["pid_path"]).write_text(str(child.pid), encoding="ascii")
    return "malformed"


def _crashing_grandchild_processor(task: dict) -> bool:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(task["payload"]["pid_path"]).write_text(str(child.pid), encoding="ascii")
    raise RuntimeError("processor crash")


def _kill_test_process(pid: int) -> None:
    if not memory_queue._pid_is_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        os.kill(pid, signal.SIGKILL)


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
        self.exitcode = None

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
    # The deadline has to outlast process creation. Spawning a Python child on
    # a hosted Windows runner takes about a second, and a 0.2 s deadline landed
    # while the child was still starting: the call then reported the wreckage
    # of a child that never ran (`process_cleanup_failed`) instead of the
    # deadline this case is about.
    deadline_seconds = 5.0
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        memory_queue._run_processor_child(
            _sleep_processor,
            {"payload": {"seconds": 120}},
            deadline_seconds,
        )

    elapsed = time.monotonic() - started
    assert deadline_seconds <= elapsed < deadline_seconds + 15


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
    try:
        assert not memory_queue._pid_is_alive(pid)
    finally:
        _kill_test_process(pid)


def test_worker_cleans_grandchild_before_returning_normal_result(tmp_path: Path) -> None:
    pid_path = tmp_path / "normal-grandchild.pid"

    try:
        result = memory_queue._run_processor_child(
            _exiting_grandchild_processor,
            {"payload": {"pid_path": str(pid_path)}},
            60,
        )
        pid = int(pid_path.read_text(encoding="ascii"))
        assert result is True
        assert not memory_queue._pid_is_alive(pid)
    finally:
        if pid_path.exists():
            _kill_test_process(int(pid_path.read_text(encoding="ascii")))


def test_worker_cleans_grandchild_before_reporting_malformed_result(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "malformed-grandchild.pid"

    try:
        with pytest.raises(memory_queue.QueueOperationError) as raised:
            memory_queue._run_processor_child(
                _malformed_grandchild_processor,
                {"payload": {"pid_path": str(pid_path)}},
                60,
            )
        pid = int(pid_path.read_text(encoding="ascii"))
        assert raised.value.code == "processor_result_malformed"
        assert not memory_queue._pid_is_alive(pid)
    finally:
        if pid_path.exists():
            _kill_test_process(int(pid_path.read_text(encoding="ascii")))


def test_worker_cleans_grandchild_before_reporting_processor_crash(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "crash-grandchild.pid"

    try:
        with pytest.raises(memory_queue.QueueOperationError) as raised:
            memory_queue._run_processor_child(
                _crashing_grandchild_processor,
                {"payload": {"pid_path": str(pid_path)}},
                60,
            )
        pid = int(pid_path.read_text(encoding="ascii"))
        assert raised.value.code == "processor_exception"
        assert not memory_queue._pid_is_alive(pid)
    finally:
        if pid_path.exists():
            _kill_test_process(int(pid_path.read_text(encoding="ascii")))


def test_deferred_compile_completes_before_queue_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scripts" / "compile_memory.py"
    pid_path = tmp_path / "compiler.pid"
    done_path = tmp_path / "compiler.done"
    script.parent.mkdir()
    script.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TEST_COMPILE_PID']).write_text(str(os.getpid()), encoding='ascii')\n"
        "time.sleep(0.2)\n"
        "Path(os.environ['TEST_COMPILE_DONE']).write_text('done', encoding='ascii')\n",
        encoding="ascii",
    )
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("compile", 1, {})
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_COMPILE_PID", str(pid_path))
    monkeypatch.setenv("TEST_COMPILE_DONE", str(done_path))
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)

    summary = memory_queue.run_worker(
        memory_queue._manual_processor,
        max_tasks=1,
        max_seconds=10,
        idle_seconds=0,
    )

    compiler_pid = int(pid_path.read_text(encoding="ascii"))
    assert summary.succeeded == 1
    assert queue.get(task_id).state == "succeeded"
    assert done_path.read_text(encoding="ascii") == "done"
    assert not memory_queue._pid_is_alive(compiler_pid)


def test_deferred_compile_timeout_kills_compiler_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scripts" / "compile_memory.py"
    pid_path = tmp_path / "timed-out-compiler.pid"
    script.parent.mkdir()
    script.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TEST_COMPILE_PID']).write_text(str(os.getpid()), encoding='ascii')\n"
        "time.sleep(300)\n",
        encoding="ascii",
    )
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("compile", 1, {})
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_COMPILE_PID", str(pid_path))
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)

    # The budget has to outlast process spawn on the slowest supported runner,
    # or the compiler is killed before it records the PID this test kills.
    summary = memory_queue.run_worker(
        memory_queue._manual_processor,
        max_tasks=1,
        max_seconds=8,
        idle_seconds=0,
    )

    compiler_pid = int(pid_path.read_text(encoding="ascii"))
    task = queue.get(task_id)
    # The subject is the kill, so it is asserted first and unconditionally.
    assert not memory_queue._pid_is_alive(compiler_pid)
    assert summary.failed == 1
    _assert_timed_out_task(task)


def _assert_timed_out_task(task) -> None:
    """A timed-out task has two correct endings; only a silent one is wrong.

    The worker offers the task again once the process cleanup it owns has
    finished. When that cleanup misses its own budget the queue fails closed and
    blocks the task instead, under the contract
    `test_cleanup_failure_blocks_task_and_stops_worker` pins. Asserting only the
    first ending made a loaded Windows runner look like a defect: runs
    32552183417 and 32554778342 both lost a shard here on commits that changed
    nothing but documentation, and a rerun of the same job went green.
    """
    if task.state == "ready":
        assert task.error_code == "worker_timeout"
        return
    assert task.state == "blocked"
    assert task.error_code == "process_cleanup_failed"
    assert task.blocked_capability == "process_cleanup"


def test_worker_child_drains_one_megabyte_result_before_join() -> None:
    result = memory_queue._run_processor_child(
        _result_processor,
        {"payload": {"size": 1024 * 1024}},
        60,
    )

    assert isinstance(result, memory_queue.DeferredResult)
    assert len(result.data) == 1024 * 1024


def test_worker_child_drains_result_at_queue_size_limit() -> None:
    result = memory_queue._run_processor_child(
        _result_processor,
        {"payload": {"size": memory_queue._MAX_RESULT_BYTES}},
        60,
    )

    assert isinstance(result, memory_queue.DeferredResult)
    assert len(result.data) == memory_queue._MAX_RESULT_BYTES


def test_worker_child_rejects_oversize_result_without_deadlock() -> None:
    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._run_processor_child(
            _result_processor,
            {"payload": {"size": memory_queue._MAX_RESULT_BYTES + 1}},
            60,
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


@pytest.mark.parametrize("reason", ["", "x" * 4097])
def test_cli_quarantine_rejects_reason_before_database_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: str,
) -> None:
    monkeypatch.setattr(
        memory_queue,
        "_v3_queue_for_cli",
        lambda: pytest.fail("database must not open for an invalid reason"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_queue.py", "quarantine-corrupt", "task-secret", "--reason", reason],
    )

    assert memory_queue._cli() == 2
    output = capsys.readouterr().out
    assert output == '{"codes":["invalid_input"]}\n'
    if reason:
        assert reason not in output


def test_cli_quarantine_outputs_only_stable_progress_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Queue:
        def quarantine_corrupt(self, task_id, *, reason, owner):
            assert (task_id, reason, owner) == ("requested-task", "retain", "owner")
            return memory_queue.CorruptExportProgress(
                task_id=task_id,
                operation_id="corrupt-export:" + "a" * 64,
                state="quarantine_pending",
                pages_written=2,
                links_exported=1500,
                complete=False,
                code="capture_link_conflicted",
            )

    monkeypatch.setattr(memory_queue, "_v3_queue_for_cli", Queue)
    monkeypatch.setattr(memory_queue, "_repair_owner_for_cli", lambda: nullcontext("owner"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_queue.py",
            "quarantine-corrupt",
            "requested-task",
            "--reason",
            "retain",
        ],
    )

    assert memory_queue._cli() == 0
    assert json.loads(capsys.readouterr().out) == {
        "code": "capture_link_conflicted",
        "complete": False,
        "operation_id": "corrupt-export:" + "a" * 64,
        "page_count": 2,
        "state": "quarantine_pending",
        "task_id": "requested-task",
    }


def test_cli_purge_corrupt_outputs_only_stable_progress_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Queue:
        def purge_quarantined(self, task_id, *, owner):
            assert (task_id, owner) == ("requested-task", "owner")
            return memory_queue.CorruptPurgeProgress(
                task_id=task_id,
                operation_id="corrupt-purge:" + "b" * 64,
                state="purge_pending",
                pages_written=3,
                links_deleted=2500,
                complete=False,
                code="corrupt_child_retention_active",
            )

    monkeypatch.setattr(memory_queue, "_v3_queue_for_cli", Queue)
    monkeypatch.setattr(memory_queue, "_repair_owner_for_cli", lambda: nullcontext("owner"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_queue.py", "purge-corrupt", "requested-task"],
    )

    assert memory_queue._cli() == 0
    assert json.loads(capsys.readouterr().out) == {
        "code": "corrupt_child_retention_active",
        "complete": False,
        "links_deleted": 2500,
        "operation_id": "corrupt-purge:" + "b" * 64,
        "page_count": 3,
        "state": "purge_pending",
        "task_id": "requested-task",
    }


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


def test_cli_work_reports_nonzero_when_task_limit_leaves_ready_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    queue = MemoryQueue(tmp_path)
    queue.enqueue("query", 1, {"number": 1})
    queue.enqueue("query", 1, {"number": 2})
    real_run_worker = memory_queue.run_worker

    def worker(processor, **kwargs):
        del processor
        return real_run_worker(
            lambda task: True,
            processor_runner=memory_queue._run_processor_inline,
            **kwargs,
        )

    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)
    monkeypatch.setattr(memory_queue, "run_worker", worker)
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_queue.py", "work", "--max-tasks", "1", "--idle-seconds", "0"],
    )

    assert memory_queue._cli() == 1
    assert json.loads(capsys.readouterr().out)["counts"] == {
        "dead": 0,
        "failed": 0,
        "processed": 1,
        "remaining_eligible": 1,
        "skipped": 0,
        "succeeded": 1,
    }


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
    monkeypatch.setattr(memory_queue, "_tracked_descendant_pids", lambda *args: None)

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
    monkeypatch.setattr(memory_queue, "_tracked_descendant_pids", lambda *args: set())

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
    monkeypatch.setattr(memory_queue, "_tracked_descendant_pids", lambda *args: set())

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
    monkeypatch.setattr(memory_queue, "_tracked_descendant_pids", lambda *args: set())
    monkeypatch.setattr(memory_queue, "_process_group_alive", lambda pid: False)

    memory_queue._terminate_processor_child(process, platform_name="posix")

    assert not process.is_alive()


def test_cleanup_fails_when_tracked_descendant_remains_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    descendant = 7777
    monkeypatch.setattr(
        memory_queue.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    monkeypatch.setattr(
        memory_queue, "_tracked_descendant_pids", lambda *args: {descendant}
    )
    monkeypatch.setattr(
        memory_queue, "_pid_is_alive", lambda pid: pid == descendant
    )

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._terminate_processor_child(
            process, platform_name="nt", cleanup_timeout=0.01
        )

    assert raised.value.code == "process_cleanup_failed"


def test_cleanup_waits_until_tracked_descendant_is_confirmed_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    descendant = 8888
    checks = iter((True, True, False))
    monkeypatch.setattr(
        memory_queue.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    monkeypatch.setattr(
        memory_queue, "_tracked_descendant_pids", lambda *args: {descendant}
    )
    monkeypatch.setattr(
        memory_queue,
        "_pid_is_alive",
        lambda pid: next(checks) if pid == descendant else False,
    )

    memory_queue._terminate_processor_child(
        process, platform_name="nt", cleanup_timeout=1
    )

    assert not process.is_alive()


def test_cleanup_fails_closed_when_windows_child_exits_before_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    process.alive = False
    monkeypatch.setattr(memory_queue, "_tracked_descendant_pids", lambda *args: set())

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue._terminate_processor_child(process, platform_name="nt")

    assert raised.value.code == "process_cleanup_failed"


def test_cleanup_failure_blocks_task_and_stops_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    first = queue.enqueue("query", 1, {"n": 1})
    second = queue.enqueue("query", 1, {"n": 2})
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)

    def cleanup_failure(processor, task, timeout):
        del processor, task, timeout
        raise memory_queue.QueueOperationError("process_cleanup_failed")

    summary = memory_queue.run_worker(
        lambda task: True,
        max_tasks=2,
        idle_seconds=0,
        processor_runner=cleanup_failure,
    )

    blocked = queue.get(first)
    untouched = queue.get(second)
    assert summary.failed == 1
    assert blocked.state == "blocked"
    assert blocked.error_code == "process_cleanup_failed"
    assert blocked.blocked_capability == "process_cleanup"
    assert untouched.state == "ready"


def test_cleanup_failure_is_blocked_even_when_heartbeat_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("query", 1, {})
    monkeypatch.setattr(memory_queue, "_queue", lambda **kwargs: queue)

    class FailedHeartbeat:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.error = RuntimeError("heartbeat failed")

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(memory_queue, "_LeaseHeartbeat", FailedHeartbeat)

    def cleanup_failure(processor, task, timeout):
        del processor, task, timeout
        raise memory_queue.QueueOperationError("process_cleanup_failed")

    summary = memory_queue.run_worker(
        lambda task: True,
        max_tasks=2,
        idle_seconds=0,
        processor_runner=cleanup_failure,
    )

    task = queue.get(task_id)
    assert summary.failed == 1
    assert task.state == "blocked"
    assert task.error_code == "process_cleanup_failed"
    assert task.blocked_capability == "process_cleanup"


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
