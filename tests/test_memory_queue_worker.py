"""The bounded queue worker: default and overridden policy, elapsed and idle limits, handler timeouts that kill the whole child tree, result frames drained without deadlock, and cleanup failures that block the task."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from llm_wiki import queue_worker

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
import queue_core  # noqa: E402
from queue_core import (  # noqa: E402
    MemoryQueue,
)

from tests.pid_files import read_pid, write_pid  # noqa: E402
from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


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
        process.join(SHORT_TIMEOUT)
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
        process.join(SHORT_TIMEOUT)


def _result_processor(task: dict) -> queue_core.DeferredResult:
    return queue_core.DeferredResult(b"x" * task["payload"]["size"])


# Long enough for a Windows runner to start the `spawn` child and its grandchild
# and write the pid before the deadline; the 1 s it was raced the write (CI run
# 36023732204, 2026-09-24).
GRANDCHILD_DEADLINE_SECONDS = 10


def _grandchild_processor(task: dict) -> bool:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_pid(task["payload"]["pid_path"], child.pid)
    time.sleep(120)
    return True


def _exiting_grandchild_processor(task: dict) -> bool:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_pid(task["payload"]["pid_path"], child.pid)
    return True


def _malformed_grandchild_processor(task: dict) -> str:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_pid(task["payload"]["pid_path"], child.pid)
    return "malformed"


def _crashing_grandchild_processor(task: dict) -> bool:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_pid(task["payload"]["pid_path"], child.pid)
    raise RuntimeError("processor crash")


def _kill_test_process(pid: int) -> None:
    if not queue_core.pid_is_alive(pid):
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


def test_worker_defaults_are_bounded_and_process_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    for number in range(25):
        queue.enqueue("query", 1, {"number": number})
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)

    summary = queue_core.run_worker(
        lambda task: True, processor_runner=queue_core.run_processor_inline
    )

    assert summary.processed == 20
    assert summary.succeeded == 20
    assert len(queue.list_tasks(states=("ready",))) == 5


def test_worker_stops_at_elapsed_limit_without_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    queue.enqueue("query", 1, {})
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)
    times = iter((0.0, 601.0))

    summary = queue_core.run_worker(
        lambda task: True,
        monotonic=lambda: next(times),
        processor_runner=queue_core.run_processor_inline,
    )

    assert summary.processed == 0
    assert queue.list_tasks(states=("ready",))[0].state == "ready"


def test_worker_stops_after_two_idle_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    summary = queue_core.run_worker(
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
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)
    now = [0.0]

    def runner(processor, task, timeout):
        del processor, task
        now[0] += timeout
        return queue_core.DeferredResult(b"late-result")

    summary = queue_core.run_worker(
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
    now = datetime(2026, 7, 14, 12, tzinfo=UTC)
    intervals: list[float] = []

    def heartbeat_wait(stop, interval: float) -> bool:
        intervals.append(interval)
        return stop.wait(SHORT_TIMEOUT)

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
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)

    summary = queue_core.run_worker(
        lambda task: False,
        max_tasks=1,
        idle_seconds=0,
        lease_seconds=10,
        heartbeat_seconds=3,
        max_attempts=10,
        retry_base_seconds=5,
        retry_cap_seconds=7,
        processor_runner=queue_core.run_processor_inline,
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
        queue_core.run_processor_child(
            _sleep_processor,
            {"payload": {"seconds": 120}},
            deadline_seconds,
        )

    elapsed = time.monotonic() - started
    assert deadline_seconds <= elapsed < deadline_seconds + 15


def test_worker_timeout_kills_spawned_grandchild_tree(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    with pytest.raises(TimeoutError):
        queue_core.run_processor_child(
            _grandchild_processor,
            {"payload": {"pid_path": str(pid_path)}},
            GRANDCHILD_DEADLINE_SECONDS,
        )
    pid = read_pid(pid_path, SHORT_TIMEOUT)
    try:
        assert not queue_core.pid_is_alive(pid)
    finally:
        _kill_test_process(pid)


def test_worker_cleans_grandchild_before_returning_normal_result(tmp_path: Path) -> None:
    pid_path = tmp_path / "normal-grandchild.pid"

    try:
        result = queue_core.run_processor_child(
            _exiting_grandchild_processor,
            {"payload": {"pid_path": str(pid_path)}},
            60,
        )
        pid = read_pid(pid_path, SHORT_TIMEOUT)
        assert result is True
        assert not queue_core.pid_is_alive(pid)
    finally:
        if pid_path.exists():
            _kill_test_process(int(pid_path.read_text(encoding="ascii")))


def test_worker_cleans_grandchild_before_reporting_malformed_result(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "malformed-grandchild.pid"

    try:
        with pytest.raises(queue_core.QueueOperationError) as raised:
            queue_core.run_processor_child(
                _malformed_grandchild_processor,
                {"payload": {"pid_path": str(pid_path)}},
                60,
            )
        pid = read_pid(pid_path, SHORT_TIMEOUT)
        assert raised.value.code == "processor_result_malformed", str(raised.value)
        assert not queue_core.pid_is_alive(pid)
    finally:
        if pid_path.exists():
            _kill_test_process(int(pid_path.read_text(encoding="ascii")))


def test_worker_cleans_grandchild_before_reporting_processor_crash(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "crash-grandchild.pid"

    try:
        with pytest.raises(queue_core.QueueOperationError) as raised:
            queue_core.run_processor_child(
                _crashing_grandchild_processor,
                {"payload": {"pid_path": str(pid_path)}},
                60,
            )
        pid = read_pid(pid_path, SHORT_TIMEOUT)
        assert raised.value.code == "processor_exception", str(raised.value)
        assert not queue_core.pid_is_alive(pid)
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
        "staged = Path(os.environ['TEST_COMPILE_PID'] + '.tmp')\n"
        "staged.write_text(str(os.getpid()), encoding='ascii')\n"
        "os.replace(staged, os.environ['TEST_COMPILE_PID'])\n"
        "time.sleep(0.2)\n"
        "Path(os.environ['TEST_COMPILE_DONE']).write_text('done', encoding='ascii')\n",
        encoding="ascii",
    )
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("compile", 1, {})
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_COMPILE_PID", str(pid_path))
    monkeypatch.setenv("TEST_COMPILE_DONE", str(done_path))
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)

    summary = queue_core.run_worker(
        memory_queue.manual_processor,
        max_tasks=1,
        max_seconds=10,
        idle_seconds=0,
    )

    compiler_pid = read_pid(pid_path, SHORT_TIMEOUT)
    assert summary.succeeded == 1
    assert queue.get(task_id).state == "succeeded"
    assert done_path.read_text(encoding="ascii") == "done"
    assert not queue_core.pid_is_alive(compiler_pid)


def test_deferred_compile_timeout_kills_compiler_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scripts" / "compile_memory.py"
    pid_path = tmp_path / "timed-out-compiler.pid"
    script.parent.mkdir()
    script.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "staged = Path(os.environ['TEST_COMPILE_PID'] + '.tmp')\n"
        "staged.write_text(str(os.getpid()), encoding='ascii')\n"
        "os.replace(staged, os.environ['TEST_COMPILE_PID'])\n"
        "time.sleep(300)\n",
        encoding="ascii",
    )
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("compile", 1, {})
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_COMPILE_PID", str(pid_path))
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)

    # The budget has to outlast process spawn on the slowest supported runner,
    # or the compiler is killed before it records the PID this test kills.
    summary = queue_core.run_worker(
        memory_queue.manual_processor,
        max_tasks=1,
        max_seconds=8,
        idle_seconds=0,
    )

    compiler_pid = read_pid(pid_path, SHORT_TIMEOUT)
    task = queue.get(task_id)
    # The subject is the kill, so it is asserted first and unconditionally.
    assert not queue_core.pid_is_alive(compiler_pid)
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
    result = queue_core.run_processor_child(
        _result_processor,
        {"payload": {"size": 1024 * 1024}},
        60,
    )

    assert isinstance(result, queue_core.DeferredResult)
    assert len(result.data) == 1024 * 1024


def test_worker_child_drains_result_at_queue_size_limit() -> None:
    result = queue_core.run_processor_child(
        _result_processor,
        {"payload": {"size": queue_core.MAX_RESULT_BYTES}},
        60,
    )

    assert isinstance(result, queue_core.DeferredResult)
    assert len(result.data) == queue_core.MAX_RESULT_BYTES


def test_worker_child_rejects_oversize_result_without_deadlock() -> None:
    with pytest.raises(queue_core.QueueOperationError) as raised:
        queue_core.run_processor_child(
            _result_processor,
            {"payload": {"size": queue_core.MAX_RESULT_BYTES + 1}},
            60,
        )

    assert raised.value.code == "processor_result_oversize"


@pytest.mark.parametrize("frame", [b"", b"unknown", b"Ttrailing", b"Eextra"])
def test_worker_child_rejects_malformed_ipc_frame(frame: bytes) -> None:
    with pytest.raises(queue_core.QueueOperationError) as raised:
        queue_core.decode_processor_frame(frame)

    assert raised.value.code == "processor_result_malformed", str(raised.value)


def test_idle_sleep_is_capped_by_worker_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    queue_core.run_worker(
        lambda task: True,
        max_seconds=0.25,
        idle_seconds=2,
        monotonic=lambda: now[0],
        sleep=sleep,
        processor_runner=queue_core.run_processor_inline,
    )

    assert sleeps == [0.25]


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
        queue_core.run_worker(
            lambda task: True,
            processor_runner=queue_core.run_processor_inline,
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

    assert memory_queue.cli() == 2
    captured = capsys.readouterr()
    assert captured.out == '{"code":"invalid_arguments","ok":false}\n'
    assert captured.err == ""
    assert "secret" not in captured.out


def test_cleanup_failure_blocks_task_and_stops_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = MemoryQueue(tmp_path)
    first = queue.enqueue("query", 1, {"n": 1})
    second = queue.enqueue("query", 1, {"n": 2})
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)

    def cleanup_failure(processor, task, timeout):
        del processor, task, timeout
        raise queue_core.QueueOperationError("process_cleanup_failed")

    summary = queue_core.run_worker(
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
    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)

    class FailedHeartbeat:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.error = RuntimeError("heartbeat failed")

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(queue_worker, "LeaseHeartbeat", FailedHeartbeat)

    def cleanup_failure(processor, task, timeout):
        del processor, task, timeout
        raise queue_core.QueueOperationError("process_cleanup_failed")

    summary = queue_core.run_worker(
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
