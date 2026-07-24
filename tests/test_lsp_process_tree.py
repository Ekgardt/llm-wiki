"""Cross-platform process-tree ownership tests for LSP children."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

import lsp_process_tree
import pytest
from lsp_process_tree import ProcessTree

FAKE_SERVER = Path(__file__).with_name("fake_lsp_server.py").resolve()


def _command(*arguments: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *arguments]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _descendant(tree: ProcessTree) -> int:
    assert tree.process.stdout is not None
    line = tree.process.stdout.readline()
    record = json.loads(line)
    return int(record["descendant_pid"])


def test_public_process_tree_shape_is_exact() -> None:
    assert [(field.name, field.type) for field in dataclasses.fields(ProcessTree)] == [
        ("process", "subprocess.Popen[bytes]"),
        ("windows_job", "int | None"),
        ("process_group", "int | None"),
    ]
    assert ProcessTree.__slots__ == ("process", "windows_job", "process_group")


def test_spawn_owns_a_real_descendant_and_terminate_leaves_no_surviving_pid(
    tmp_path: Path,
) -> None:
    tree = ProcessTree.spawn(
        _command("--spawn-descendant", "--sleep-seconds", "30"),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    descendant_pid = _descendant(tree)
    assert _pid_alive(tree.process.pid)
    assert _pid_alive(descendant_pid)

    tree.terminate(deadline=time.monotonic() + 5)
    tree.close()

    assert tree.process.poll() is not None
    deadline = time.monotonic() + 2
    while _pid_alive(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_alive(descendant_pid)


def test_close_is_release_only_and_retains_a_live_owned_tree(tmp_path: Path) -> None:
    tree = ProcessTree.spawn(
        _command("--spawn-descendant", "--sleep-seconds", "30"),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    descendant_pid = _descendant(tree)

    with pytest.raises(RuntimeError, match="live|reaped|empty"):
        tree.close()

    assert tree.process_group is not None or tree.windows_job is not None
    assert tree.process.poll() is None
    assert _pid_alive(descendant_pid)

    tree.terminate(deadline=time.monotonic() + 5)
    tree.close()
    tree.close()

    deadline = time.monotonic() + 2
    while _pid_alive(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_alive(descendant_pid)


def test_terminate_cleans_descendant_after_direct_leader_already_exited(
    tmp_path: Path,
) -> None:
    tree = ProcessTree.spawn(
        _command("--spawn-descendant", "--exit-after-descendant-spawn"),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    descendant_pid = _descendant(tree)
    tree.process.wait(timeout=5)
    assert _pid_alive(descendant_pid)

    try:
        tree.terminate(deadline=time.monotonic() + 5)
        deadline = time.monotonic() + 2
        while _pid_alive(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_alive(descendant_pid)
    finally:
        tree.close()


def test_close_retains_group_until_descendant_after_exited_leader_is_gone(
    tmp_path: Path,
) -> None:
    tree = ProcessTree.spawn(
        _command("--spawn-descendant", "--exit-after-descendant-spawn"),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    descendant_pid = _descendant(tree)
    tree.process.wait(timeout=5)
    assert _pid_alive(descendant_pid)

    with pytest.raises(RuntimeError, match="live|empty"):
        tree.close()

    assert tree.process_group is not None or tree.windows_job is not None
    tree.terminate(deadline=time.monotonic() + 5)
    tree.close()

    deadline = time.monotonic() + 2
    while _pid_alive(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_alive(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended assignment boundary")
def test_assignment_failure_kills_unassigned_suspended_child_within_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[object] = []
    jobs: list[int] = []
    closed: list[int] = []
    real_popen = lsp_process_tree.subprocess.Popen
    real_create_job = lsp_process_tree._create_windows_job
    real_close = lsp_process_tree._close_windows_handle

    def popen(*args: object, **kwargs: object) -> object:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def create_job() -> int:
        job = real_create_job()
        jobs.append(job)
        return job

    def close_handle(handle: int) -> None:
        closed.append(handle)
        real_close(handle)

    monkeypatch.setattr(lsp_process_tree.subprocess, "Popen", popen)
    monkeypatch.setattr(lsp_process_tree, "_create_windows_job", create_job)
    monkeypatch.setattr(lsp_process_tree, "_close_windows_handle", close_handle)
    monkeypatch.setattr(
        lsp_process_tree,
        "_assign_windows_process",
        lambda _job, _process: (_ for _ in ()).throw(OSError("assignment failed")),
    )
    started = time.monotonic()

    with pytest.raises(OSError, match="assignment failed"):
        ProcessTree.spawn(
            _command("--sleep-seconds", "30"), cwd=tmp_path, env=dict(os.environ)
        )

    assert time.monotonic() - started <= 2.25
    assert len(children) == 1
    assert children[0].poll() is not None
    assert jobs and closed.count(jobs[0]) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended assignment boundary")
def test_assignment_failure_still_kills_child_when_job_close_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Child:
        pid = 424242
        stdin = None
        stdout = None
        stderr = None

        def __init__(self) -> None:
            self.alive = True
            self.kill_calls = 0
            self.wait_calls = 0

        def poll(self) -> int | None:
            return None if self.alive else 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.alive = False

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            return 1

    class Kernel:
        @staticmethod
        def TerminateJobObject(_job: int, _code: int) -> int:
            return 1

    child = Child()
    monkeypatch.setattr(lsp_process_tree, "_KERNEL32", Kernel())
    monkeypatch.setattr(lsp_process_tree, "_create_windows_job", lambda: 17)
    monkeypatch.setattr(lsp_process_tree.subprocess, "Popen", lambda *_a, **_kw: child)
    monkeypatch.setattr(
        lsp_process_tree,
        "_assign_windows_process",
        lambda _job, _process: (_ for _ in ()).throw(OSError("assignment failed")),
    )
    monkeypatch.setattr(
        lsp_process_tree,
        "_close_windows_handle",
        lambda _job: (_ for _ in ()).throw(RuntimeError("job close failed")),
    )
    monkeypatch.setattr(lsp_process_tree, "_job_active_processes", lambda _job: 0)

    with pytest.raises(lsp_process_tree._ProcessTreeSpawnError) as raised:
        ProcessTree.spawn(_command(), cwd=tmp_path, env=dict(os.environ))

    assert isinstance(raised.value.__cause__, OSError)
    assert "assignment failed" in str(raised.value.__cause__)
    assert raised.value.tree.windows_job == 17
    assert any("job close failed" in str(error) for error in raised.value.errors)
    assert child.kill_calls == 1
    assert child.wait_calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-child Job ownership")
def test_popen_failure_retains_job_when_job_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process_tree, "_create_windows_job", lambda: 19)
    monkeypatch.setattr(
        lsp_process_tree.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("popen failed")),
    )
    monkeypatch.setattr(
        lsp_process_tree,
        "_close_windows_handle",
        lambda _job: (_ for _ in ()).throw(OSError("job close failed")),
    )

    with pytest.raises(lsp_process_tree._ProcessTreeSpawnError) as raised:
        ProcessTree.spawn(_command(), cwd=tmp_path, env=dict(os.environ))

    assert isinstance(raised.value.__cause__, OSError)
    assert "popen failed" in str(raised.value.__cause__)
    assert raised.value.tree is None
    assert raised.value.windows_job == 19
    assert any("job close failed" in str(error) for error in raised.value.errors)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job setup ownership")
def test_job_configuration_failure_retains_job_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Kernel:
        @staticmethod
        def CreateJobObjectW(_security: object, _name: object) -> int:
            return 41

        @staticmethod
        def SetInformationJobObject(
            _job: int,
            _kind: int,
            _information: object,
            _size: int,
        ) -> int:
            return 0

        @staticmethod
        def CloseHandle(_job: int) -> int:
            return 0

    monkeypatch.setattr(lsp_process_tree, "_KERNEL32", Kernel())
    monkeypatch.setattr(lsp_process_tree.ctypes, "get_last_error", lambda: 5)

    with pytest.raises(lsp_process_tree._ProcessTreeSpawnError) as raised:
        lsp_process_tree._create_windows_job()

    assert raised.value.tree is None
    assert raised.value.windows_job == 41
    assert len(raised.value.errors) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows final child ownership proof")
def test_setup_rollback_retains_tree_when_final_child_poll_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Child:
        pid = 9191
        stdin = None
        stdout = None
        stderr = None

        @staticmethod
        def poll() -> int:
            raise OSError("final poll failed")

    child = Child()
    monkeypatch.setattr(lsp_process_tree, "_create_windows_job", lambda: 43)
    monkeypatch.setattr(
        lsp_process_tree.subprocess,
        "Popen",
        lambda *_args, **_kwargs: child,
    )
    monkeypatch.setattr(
        lsp_process_tree,
        "_assign_windows_process",
        lambda _job, _process: (_ for _ in ()).throw(OSError("assignment failed")),
    )
    monkeypatch.setattr(
        ProcessTree,
        "terminate",
        lambda self, *, deadline: None,
    )

    def release_job(tree: ProcessTree) -> None:
        tree.windows_job = None

    monkeypatch.setattr(ProcessTree, "close", release_job)

    with pytest.raises(lsp_process_tree._ProcessTreeSpawnError) as raised:
        ProcessTree.spawn(_command(), cwd=tmp_path, env=dict(os.environ))

    assert raised.value.tree is not None
    assert raised.value.tree.process is child
    assert any("final poll failed" in str(error) for error in raised.value.errors)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, "later"])
def test_terminate_rejects_invalid_deadline_without_releasing_tree(
    tmp_path: Path, deadline: object
) -> None:
    tree = ProcessTree.spawn(
        _command("--sleep-seconds", "30"), cwd=tmp_path, env=dict(os.environ)
    )
    try:
        with pytest.raises((TypeError, ValueError)):
            tree.terminate(deadline=deadline)  # type: ignore[arg-type]
        assert tree.process.poll() is None
    finally:
        tree.terminate(deadline=time.monotonic() + 5)
        tree.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group algorithm")
def test_posix_wait_reaps_zombie_leader_before_each_group_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    probes = iter([True, False, False])

    class ZombieLeader:
        pid = 4242

        def poll(self) -> int:
            events.append("poll")
            return 0

    def killpg(_group: int, sig: int) -> None:
        if sig == 0:
            events.append("probe")
            if not next(probes):
                raise ProcessLookupError
        else:
            events.append(f"signal:{sig}")

    monkeypatch.setattr(lsp_process_tree.os, "killpg", killpg)
    monkeypatch.setattr(lsp_process_tree.time, "sleep", lambda _seconds: None)
    tree = ProcessTree(ZombieLeader(), None, 4242)  # type: ignore[arg-type]

    tree.terminate(deadline=time.monotonic() + 1)

    probe_indexes = [index for index, event in enumerate(events) if event == "probe"]
    assert probe_indexes
    assert all(events[index - 1] == "poll" for index in probe_indexes)
    assert tree.process_group == 4242
    tree.close()
    assert tree.process_group is None


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process contract")
@pytest.mark.parametrize("previous_count", [0, 1, 2, 0xFFFFFFFF])
def test_windows_resume_accepts_only_previous_suspend_count_one(
    monkeypatch: pytest.MonkeyPatch,
    previous_count: int,
) -> None:
    import ctypes

    pid = 4242

    class Kernel:
        @staticmethod
        def CreateToolhelp32Snapshot(_flags: int, _pid: int) -> int:
            return 11

        @staticmethod
        def Thread32First(_snapshot: int, pointer: object) -> int:
            entry = ctypes.cast(
                pointer, ctypes.POINTER(lsp_process_tree._ThreadEntry32)
            ).contents
            entry.owner_process_id = pid
            entry.thread_id = 22
            return 1

        @staticmethod
        def Thread32Next(_snapshot: int, _pointer: object) -> int:
            ctypes.set_last_error(18)
            return 0

        @staticmethod
        def OpenThread(_access: int, _inherit: bool, _thread_id: int) -> int:
            return 33

        @staticmethod
        def ResumeThread(_thread: int) -> int:
            return previous_count

    monkeypatch.setattr(lsp_process_tree, "_KERNEL32", Kernel())
    monkeypatch.setattr(lsp_process_tree, "_close_windows_handle", lambda _handle: None)

    if previous_count == 1:
        lsp_process_tree._resume_windows_process(pid)
    else:
        with pytest.raises((OSError, RuntimeError)):
            lsp_process_tree._resume_windows_process(pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows thread enumeration contract")
def test_windows_resume_rejects_thread_enumeration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    pid = 4242
    closed: list[int] = []

    class Kernel:
        @staticmethod
        def CreateToolhelp32Snapshot(_flags: int, _pid: int) -> int:
            return 11

        @staticmethod
        def Thread32First(_snapshot: int, pointer: object) -> int:
            entry = ctypes.cast(
                pointer, ctypes.POINTER(lsp_process_tree._ThreadEntry32)
            ).contents
            entry.owner_process_id = pid
            entry.thread_id = 22
            return 1

        @staticmethod
        def Thread32Next(_snapshot: int, _pointer: object) -> int:
            ctypes.set_last_error(5)
            return 0

        @staticmethod
        def OpenThread(_access: int, _inherit: bool, _thread_id: int) -> int:
            return 33

        @staticmethod
        def ResumeThread(_thread: int) -> int:
            return 1

    monkeypatch.setattr(lsp_process_tree, "_KERNEL32", Kernel())
    monkeypatch.setattr(
        lsp_process_tree, "_close_windows_handle", lambda handle: closed.append(handle)
    )

    with pytest.raises(OSError) as raised:
        lsp_process_tree._resume_windows_process(pid)

    assert raised.value.winerror == 5
    assert closed == [33, 11]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job cleanup contract")
def test_windows_terminate_attempts_direct_kill_when_job_termination_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Child:
        pid = 5151

        def __init__(self) -> None:
            self.alive = True

        def poll(self) -> int | None:
            calls.append("poll")
            return None if self.alive else 1

        def kill(self) -> None:
            calls.append("kill")
            self.alive = False

        def wait(self, timeout: float | None = None) -> int:
            calls.append("wait")
            self.alive = False
            return 1

    class Kernel:
        @staticmethod
        def TerminateJobObject(_job: int, _code: int) -> int:
            calls.append("terminate-job")
            return 0

    child = Child()
    tree = ProcessTree(child, 17, None)  # type: ignore[arg-type]
    monkeypatch.setattr(lsp_process_tree, "_KERNEL32", Kernel())
    monkeypatch.setattr(lsp_process_tree.ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(lsp_process_tree, "_job_active_processes", lambda _job: 0)

    with pytest.raises(OSError):
        tree.terminate(deadline=time.monotonic() + 1)

    assert "terminate-job" in calls
    assert "kill" in calls
    assert "wait" in calls or child.poll() is not None
    assert tree.windows_job == 17


@pytest.mark.skipif(os.name != "nt", reason="Windows independent cleanup matrix")
@pytest.mark.parametrize("failure", ["terminate", "poll", "kill", "wait"])
def test_windows_terminate_attempts_every_independent_step_after_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    calls: list[str] = []

    class Child:
        pid = 7171

        def __init__(self) -> None:
            self.alive = True
            self.poll_calls = 0

        def poll(self) -> int | None:
            calls.append("poll")
            self.poll_calls += 1
            if failure == "poll" and self.poll_calls == 1:
                raise OSError("poll failed")
            return None if self.alive else 1

        def kill(self) -> None:
            calls.append("kill")
            if failure == "kill":
                raise OSError("kill failed")
            self.alive = False

        def wait(self, timeout: float | None = None) -> int:
            calls.append("wait")
            if failure == "wait":
                raise OSError("wait failed")
            self.alive = False
            return 1

    class Kernel:
        @staticmethod
        def TerminateJobObject(_job: int, _code: int) -> int:
            calls.append("terminate-job")
            if failure == "terminate":
                raise OSError("job termination failed")
            return 1

    child = Child()
    tree = ProcessTree(child, 29, None)  # type: ignore[arg-type]

    def active_processes(_job: int) -> int:
        calls.append("query-job")
        return 0

    monkeypatch.setattr(lsp_process_tree, "_KERNEL32", Kernel())
    monkeypatch.setattr(lsp_process_tree, "_job_active_processes", active_processes)

    with pytest.raises(OSError, match="failed"):
        tree.terminate(deadline=time.monotonic() + 1)

    assert calls.count("terminate-job") == 1
    assert calls.count("kill") == 1
    assert calls.count("wait") >= 1
    assert calls.count("query-job") >= 1
    assert tree.windows_job == 29


@pytest.mark.skipif(os.name != "nt", reason="Windows Job release contract")
def test_windows_close_failure_retains_job_handle_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapedChild:
        pid = 6161

        @staticmethod
        def poll() -> int:
            return 0

    tree = ProcessTree(ReapedChild(), 23, None)  # type: ignore[arg-type]
    monkeypatch.setattr(lsp_process_tree, "_job_active_processes", lambda _job: 0)
    monkeypatch.setattr(
        lsp_process_tree,
        "_close_windows_handle",
        lambda _job: (_ for _ in ()).throw(OSError("job close failed")),
    )

    with pytest.raises(OSError, match="job close failed"):
        tree.close()

    assert tree.windows_job == 23
    monkeypatch.setattr(lsp_process_tree, "_close_windows_handle", lambda _job: None)
    tree.close()
    assert tree.windows_job is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job release observations")
def test_windows_close_queries_job_when_direct_child_observation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Child:
        pid = 8181

        @staticmethod
        def poll() -> int:
            calls.append("poll")
            raise OSError("poll failed")

    def active_processes(_job: int) -> int:
        calls.append("query-job")
        return 0

    tree = ProcessTree(Child(), 31, None)  # type: ignore[arg-type]
    monkeypatch.setattr(lsp_process_tree, "_job_active_processes", active_processes)

    with pytest.raises(OSError, match="poll failed"):
        tree.close()

    assert calls == ["poll", "query-job"]
    assert tree.windows_job == 31
