from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import cast

import lsp_process_tree
import pytest
import windows_workspace
from lsp_process_tree import ProcessTree, ProcessTreeSpawnError

JOB = 7
RelativeCall = tuple[int, str, dict[str, object]]


class _Kernel:
    def __init__(self, terminate_error: BaseException | None = None) -> None:
        self.terminate_error = terminate_error
        self.calls: list[tuple[str, int]] = []

    def TerminateJobObject(self, job: int, code: int) -> int:
        self.calls.append(("terminate", job))
        _raise_if_set(self.terminate_error)
        return 1

    def CloseHandle(self, handle: int) -> int:
        self.calls.append(("close", handle))
        return 1

    def AssignProcessToJobObject(self, job: int, process_handle: int) -> int:
        self.calls.append(("assign", process_handle))
        return 1


def _raise_if_set(error: BaseException | None) -> None:
    if error is not None:
        raise error


class _ReapedProcess:
    pid = 4242
    _handle = 31

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _process() -> subprocess.Popen[bytes]:
    return cast("subprocess.Popen[bytes]", _ReapedProcess())


def _no_pids(job: int) -> tuple[int, ...]:
    return ()


def _tree_gone(
    process: object, job: int, deadline: float, *, tracked_pids: tuple[int, ...]
) -> tuple[bool, list[BaseException]]:
    return True, []


def _no_active(job: int) -> int:
    return 0


def _close_refused(handle: int) -> None:
    raise OSError("close refused")


def _windows_seams(monkeypatch: pytest.MonkeyPatch, kernel: _Kernel) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(lsp_process_tree, "KERNEL32", kernel, raising=False)
    monkeypatch.setattr(lsp_process_tree, "job_process_ids", _no_pids)
    monkeypatch.setattr(lsp_process_tree, "wait_windows_tree", _tree_gone)
    monkeypatch.setattr(lsp_process_tree, "_bounded_job_active_processes", _no_active)


def test_a_failed_windows_setup_tears_the_tree_down_and_reraises_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _Kernel()
    _windows_seams(monkeypatch, kernel)
    cause = OSError("assignment failed")

    with pytest.raises(OSError) as raised:
        ProcessTree._raise_started_windows_failure(_process(), JOB, 10.0**9, cause)

    assert raised.value is cause
    assert kernel.calls == [("terminate", JOB), ("close", JOB)]


def test_a_failed_windows_setup_whose_job_stays_open_reports_the_retained_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _windows_seams(monkeypatch, _Kernel())
    monkeypatch.setattr(lsp_process_tree, "close_windows_handle", _close_refused)
    cause = OSError("assignment failed")

    with pytest.raises(ProcessTreeSpawnError) as raised:
        ProcessTree._raise_started_windows_failure(_process(), JOB, 10.0**9, cause)

    assert raised.value.windows_job == JOB
    assert raised.value.__cause__ is cause
    assert [str(error) for error in raised.value.errors] == ["close refused"]


def test_a_windows_job_that_cannot_be_terminated_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[BaseException] = []
    failure = OSError("terminate refused")
    monkeypatch.setattr(lsp_process_tree, "KERNEL32", _Kernel(failure), raising=False)

    lsp_process_tree._terminate_windows_job(JOB, errors)

    assert errors == [failure]


def test_a_windows_setup_without_a_child_closes_only_its_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(lsp_process_tree, "close_windows_handle", closed.append)
    cause = OSError("creation failed")

    with pytest.raises(OSError) as raised:
        ProcessTree._raise_unstarted_windows_failure(JOB, cause)

    assert raised.value is cause
    assert closed == [JOB]


def test_a_windows_setup_without_a_child_whose_job_stays_open_names_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lsp_process_tree, "close_windows_handle", _close_refused)
    cause = OSError("creation failed")

    with pytest.raises(ProcessTreeSpawnError) as raised:
        ProcessTree._raise_unstarted_windows_failure(JOB, cause)

    assert (raised.value.tree, raised.value.windows_job) == (None, JOB)
    assert raised.value.__cause__ is cause


def test_a_windows_process_is_assigned_by_its_own_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _Kernel()
    monkeypatch.setattr(lsp_process_tree, "KERNEL32", kernel, raising=False)

    lsp_process_tree.assign_windows_process(JOB, _process())

    assert kernel.calls == [("assign", 31)]


def _alive(pid: int) -> bool:
    return True


def _unreadable(pid: int) -> bool:
    raise OSError("access denied")


@pytest.mark.parametrize(("probe", "expected"), [(_alive, True), (_unreadable, False)])
def test_a_tracked_windows_pid_that_cannot_be_read_stops_being_tracked(
    monkeypatch: pytest.MonkeyPatch, probe: Callable[[int], bool], expected: bool
) -> None:
    monkeypatch.setattr(lsp_process_tree, "windows_pid_alive", probe)

    assert lsp_process_tree._pid_still_alive(4242) is expected


def _recorded_relative_handle(calls: list[RelativeCall]) -> Callable[..., int]:
    def relative_handle(parent: int, name: str, **options: object) -> int:
        calls.append((parent, name, options))
        return 99

    return relative_handle


def _directory(create: bool, **extra: object) -> dict[str, object]:
    return {"directory": True, "create": create, **extra}


def _file(create: bool, **extra: object) -> dict[str, object]:
    return {"directory": False, "create": create, **extra}


@pytest.mark.parametrize(
    ("opener", "options"),
    [
        (windows_workspace.create_directory, _directory(True)),
        (windows_workspace.create_writable_directory, _directory(True, writable=True)),
        (windows_workspace.open_directory, _directory(False)),
        (windows_workspace.create_file, _file(True, writable=True)),
        (windows_workspace.open_file, _file(False)),
        (windows_workspace.open_deletable_file, _file(False, deletable=True)),
        (windows_workspace.open_deletable_directory, _directory(False, deletable=True)),
    ],
)
def test_each_windows_opener_asks_for_its_own_kind_of_handle(
    monkeypatch: pytest.MonkeyPatch,
    opener: Callable[[int, str], int],
    options: dict[str, object],
) -> None:
    calls: list[RelativeCall] = []
    monkeypatch.setattr(windows_workspace, "relative_handle", _recorded_relative_handle(calls))

    assert opener(5, "child") == 99
    assert calls == [(5, "child", options)]
