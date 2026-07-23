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


def test_close_is_idempotent_and_kills_the_owned_tree(tmp_path: Path) -> None:
    tree = ProcessTree.spawn(
        _command("--spawn-descendant", "--sleep-seconds", "30"),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    descendant_pid = _descendant(tree)

    tree.close()
    tree.close()
    tree.process.wait(timeout=5)

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


def test_close_cleans_descendant_after_direct_leader_already_exited(
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
        tree.close()
        tree.process.wait(timeout=5)
