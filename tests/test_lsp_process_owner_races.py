"""Owner-root races, Windows handles and native scratch stay owned by one LSP child."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import compile_cache
import pytest
import windows_workspace
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_generation,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_startup,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)

from tests.lsp_process_helpers import (
    _lsp_threads,
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _STARTUP_CEILING_SECONDS,
    OWNER_NONCE,
    _all_children_reaped,
    _assert_lsp_acl_is_owner_only,
    _command,
    _coordinator_wait,
    _expect_active_generation_exit,
    _FakeTree,
    _start,
    _wait,
)
from tests.slow_machine import SHORT_TIMEOUT


def _assert_protocol_startup_failure(error: BaseException) -> None:
    """Either the plain protocol failure, or one wrapping it with retryable cleanup."""
    if isinstance(error, lsp_process_cleanup.StartupCleanupError):
        assert isinstance(error.__cause__, RuntimeError)
        assert str(error.__cause__) == "protocol startup failed"
        error.retry_cleanup(time.monotonic() + 5)
        return
    assert str(error) == "protocol startup failed"


def _assert_startup_failure_evidence(owner: Path, tmp_path: Path) -> None:
    evidence = (owner / "failure.json").read_bytes()
    assert json.loads(evidence)["code"] == "startup_failed"
    assert str(tmp_path).encode() not in evidence
    assert b"sleep-seconds" not in evidence
    _assert_lsp_acl_is_owner_only(owner / "failure.json", inherited=True)


def _await_thread_names(expected: set[str], timeout: float = 2.0) -> set[str]:
    deadline = time.monotonic() + timeout
    current = _lsp_thread_names()
    while current != expected and time.monotonic() < deadline:
        time.sleep(0.01)
        current = _lsp_thread_names()
    return current


def _lsp_thread_names() -> set[str]:
    return {thread.name for thread in _lsp_threads()}


def _threads_for_nonces(nonces: set[str]) -> list[str]:
    """LSP threads still named after one of these owner nonces."""
    return [
        thread.name
        for thread in _lsp_threads()
        if any(nonce in thread.name for nonce in nonces)
    ]


def _windows_process_handle_count() -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(
        kernel32.GetCurrentProcess(), ctypes.byref(count)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(count.value)


def test_real_sleeping_child_startup_failure_is_cleaned_within_one_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="protocol startup failed"):
        LspProcess.start(
            _command("--sleep-seconds", "30"),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
        )
    elapsed = time.monotonic() - started
    assert elapsed < _STARTUP_CEILING_SECONDS
    assert len(children) == 1
    assert children[0].poll() is not None


def test_permanently_alive_child_preserves_evidence_without_pipe_close_or_join(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BlockingClose(io.BytesIO):
        cleanup_allowed = False

        def close(self) -> None:
            if not self.cleanup_allowed:
                raise AssertionError("pipe close must not run while child is alive")
            super().close()

    class PermanentlyAliveProcess:
        args = _command("--ignored-secret", "secret")
        pid = 525252
        stdin = BlockingClose()
        stdout = BlockingClose()
        stderr = BlockingClose()
        cleanup_allowed = False
        alive = True

        def poll(self) -> int | None:
            return None if self.alive else 1

        def wait(self, timeout: float | None = None) -> int:
            if not self.alive:
                return 1
            assert timeout is not None
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(self.args, timeout)

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            if self.cleanup_allowed:
                self.alive = False

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    child = PermanentlyAliveProcess()
    real_thread_start = threading.Thread.start

    def skip_stderr_owner(thread: threading.Thread) -> None:
        if not thread.name.startswith("lsp-stderr-"):
            real_thread_start(thread)

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(lambda _cls, *_args, **_kwargs: _FakeTree(child)),
    )
    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(threading.Thread, "start", skip_stderr_owner)
    monkeypatch.setattr(
        lsp_process_owner, "secure_windows_owner_root", lambda _path, _deadline: None
    )
    owner = tmp_path / OWNER_NONCE
    started = time.monotonic()
    with pytest.raises(lsp_process_cleanup.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    elapsed = time.monotonic() - started
    assert elapsed < _STARTUP_CEILING_SECONDS
    assert (owner / "failure.json").is_file()
    assert (owner / "owner.json").is_file()
    child.cleanup_allowed = True
    BlockingClose.cleanup_allowed = True
    raised.value.retry_cleanup(time.monotonic() + 5)


def test_initial_owner_publish_never_overwrites_racing_owner_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attacker = b"attacker-owner-record"
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    if os.name == "nt":
        real_create = windows_workspace.create_file
        real_publish = windows_workspace.publish_file

        def race_publish(temporary_handle: int, parent: int, name: str) -> None:
            if name == "owner.json":
                attacker_handle = real_create(parent, name)
                try:
                    windows_workspace.write_all(
                        attacker_handle, attacker, chunk_bytes=4096
                    )
                    windows_workspace.flush_file(attacker_handle)
                finally:
                    windows_workspace.close_handle(attacker_handle)
            real_publish(temporary_handle, parent, name)

        monkeypatch.setattr(windows_workspace, "publish_file", race_publish)
    else:
        real_open = os.open

        def race_publish(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            return real_open(path, flags, mode, dir_fd=dir_fd)

        real_link = os.link

        def race_link(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            if destination == "owner.json":
                descriptor = real_open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(descriptor, attacker)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            real_link(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        monkeypatch.setattr(os, "link", race_link)
    monkeypatch.setattr(
        lsp_process_state.subprocess,
        "Popen",
        lambda *args, **kwargs: children.append(real_popen(*args, **kwargs)) or children[-1],
    )
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(FileExistsError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert (owner / "owner.json").read_bytes() == attacker
    assert owner.is_dir()
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None


def test_existing_failure_record_is_never_replaced_by_process_exit(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--exit-while-pending")
    attacker_record = {
        "code": "preexisting_failure",
        "generation_nonce": process.generation_nonce,
        "owner_nonce": process.owner_nonce,
        "timestamp": "2026-07-23T00:00:00Z",
    }
    attacker = json.dumps(
        attacker_record, sort_keys=True, separators=(",", ":")
    ).encode()
    failure_path = process.owner_root / "failure.json"
    failure_path.write_bytes(attacker)
    process.process.stdin.close()
    _wait(process)
    assert _coordinator_wait(process, lambda: process.restart_count == 1)
    assert process.state is ProcessState.PROCESS_RUNNING
    assert failure_path.read_bytes() == attacker
    assert json.loads((process.owner_root / "owner.json").read_bytes())["state"] == (
        "process_running"
    )
    process.close(time.monotonic() + 5)


def _replace_owner_root_with_attacker(owner: Path, moved: Path) -> tuple[Path, Path]:
    owner.rename(moved)
    owner.mkdir()
    sentinel = owner / "sentinel.txt"
    owner_record = owner / "owner.json"
    sentinel.write_bytes(b"attacker-sentinel")
    owner_record.write_bytes(b"attacker-owner")
    return sentinel, owner_record


def _swap_owner_root_unless_windows_refuses(
    owner: Path, moved: Path
) -> tuple[Path, Path] | None:
    try:
        return _replace_owner_root_with_attacker(owner, moved)
    except OSError:
        if os.name != "nt":
            raise
        return None


def _wait_and_close_process_handles(
    kernel32: object, handles: list[int], exit_results: list[int]
) -> None:
    for handle in handles:
        exit_results.append(int(kernel32.WaitForSingleObject(handle, 2000)))
        assert kernel32.CloseHandle(handle)


def test_parent_swap_during_owner_write_rejects_and_preserves_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    real_write = lsp_process_state.write_owner_record
    replacement: tuple[Path, Path] | None = None
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def write_then_swap(*args: object, **kwargs: object):
        nonlocal replacement
        result = real_write(*args, **kwargs)
        try:
            replacement = _replace_owner_root_with_attacker(owner, moved)
        except OSError:
            if os.name != "nt":
                raise
        return result

    monkeypatch.setattr(lsp_process_state, "write_owner_record", write_then_swap)
    monkeypatch.setattr(
        lsp_process_state.subprocess,
        "Popen",
        lambda *args, **kwargs: children.append(real_popen(*args, **kwargs)) or children[-1],
    )
    if os.name == "nt":
        process = LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
        _expect_active_generation_exit(process)
        process.process.stdin.close()
        _wait(process)
        assert replacement is None
        process.close(time.monotonic() + 5)
    else:
        with pytest.raises(RuntimeError, match="identity changed"):
            LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
        assert replacement is not None
        sentinel, owner_record = replacement
        assert sentinel.read_bytes() == b"attacker-sentinel"
        assert owner_record.read_bytes() == b"attacker-owner"
        assert json.loads((moved / "failure.json").read_bytes())["code"] == "startup_failed"
        assert not (owner / "failure.json").exists()
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None


def test_final_fence_rejects_post_protocol_owner_swap_without_deleting_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    real_start = threading.Thread.start
    replacement: tuple[Path, Path] | None = None
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def start_then_swap(thread: threading.Thread) -> None:
        nonlocal replacement
        real_start(thread)
        if not thread.name.startswith("lsp-exit-"):
            return
        swapped = _swap_owner_root_unless_windows_refuses(owner, moved)
        if swapped is not None:
            replacement = swapped

    monkeypatch.setattr(threading.Thread, "start", start_then_swap)
    monkeypatch.setattr(
        lsp_process_state.subprocess,
        "Popen",
        lambda *args, **kwargs: children.append(real_popen(*args, **kwargs)) or children[-1],
    )
    if os.name == "nt":
        process = LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
        _expect_active_generation_exit(process)
        process.process.stdin.close()
        _wait(process)
        assert replacement is None
        process.close(time.monotonic() + 5)
    else:
        with pytest.raises(RuntimeError, match="identity changed"):
            LspProcess.start(
                _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
            )
        assert replacement is not None
        sentinel, owner_record = replacement
        assert sentinel.read_bytes() == b"attacker-sentinel"
        assert owner_record.read_bytes() == b"attacker-owner"
        assert json.loads((moved / "failure.json").read_bytes())["code"] == "startup_failed"
    assert len(children) == 1
    children[0].wait(timeout=SHORT_TIMEOUT)
    assert children[0].poll() is not None


def test_replacement_before_startup_failure_write_is_never_mutated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    real_evidence = lsp_process_state.write_failure_record
    replacement: tuple[Path, Path] | None = None

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    def swap_before_evidence(*args: object, **kwargs: object):
        nonlocal replacement
        try:
            replacement = _replace_owner_root_with_attacker(owner, moved)
        except OSError:
            if os.name != "nt":
                raise
        return real_evidence(*args, **kwargs)

    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process_state, "write_failure_record", swap_before_evidence)
    with pytest.raises(RuntimeError, match="protocol startup failed"):
        LspProcess.start(
            _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
        )
    if os.name == "nt":
        assert replacement is None
        assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    else:
        assert replacement is not None
        sentinel, owner_record = replacement
        assert sentinel.read_bytes() == b"attacker-sentinel"
        assert owner_record.read_bytes() == b"attacker-owner"
        assert json.loads((moved / "failure.json").read_bytes())["code"] == "startup_failed"
        assert not (owner / "failure.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor boundary required")
def test_live_process_failure_evidence_follows_renamed_owner_handle(
    tmp_path: Path,
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    process = _start(tmp_path, "--exit-while-pending")

    owner.rename(moved)
    owner.mkdir()
    sentinel = owner / "sentinel.txt"
    sentinel.write_bytes(b"replacement")
    process.process.stdin.close()
    _wait(process)
    assert _coordinator_wait(process, lambda: process.state is ProcessState.FAILED)

    assert json.loads((moved / "failure.json").read_bytes())["code"] == "restart_failed"
    assert sentinel.read_bytes() == b"replacement"
    assert not (owner / "failure.json").exists()
    process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle boundary required")
def test_live_process_owner_handle_blocks_lexical_rename_on_windows(
    tmp_path: Path,
) -> None:
    owner = tmp_path / OWNER_NONCE
    process = _start(tmp_path, "--exit-while-pending")
    try:
        with pytest.raises(OSError):
            owner.rename(tmp_path / "moved-original")
    finally:
        _expect_active_generation_exit(process)
        process.process.stdin.close()
        _wait(process)

    assert (owner / "lease.json").is_file()
    process.close(time.monotonic() + 5)


def test_child_exit_during_final_startup_window_fails_start_and_retains_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_start = threading.Thread.start

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def start_after_child_exit(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-exit-"):
            children[0].wait(timeout=SHORT_TIMEOUT)
        real_start(thread)

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(threading.Thread, "start", start_after_child_exit)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="exited during (generation )?startup"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    assert len(children) == 1
    assert children[0].poll() is not None
    assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_owner_file_create_race_is_anchored_and_never_mutates_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    replacement: tuple[Path, Path, Path] | None = None

    if os.name == "nt":
        real_create = windows_workspace.create_file

        def create_after_blocked_swap(parent: int, name: str) -> int:
            if name == "owner.json":
                with pytest.raises(OSError):
                    owner.rename(moved)
            return real_create(parent, name)

        monkeypatch.setattr(
            windows_workspace, "create_file", create_after_blocked_swap
        )
    else:
        real_link = os.link

        def publish_after_swap(
            source: object,
            destination: object,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            nonlocal replacement
            if destination == "owner.json":
                owner.rename(moved)
                owner.mkdir()
                sentinel = owner / "sentinel.txt"
                owner_record = owner / "owner.json"
                failure_record = owner / "failure.json"
                sentinel.write_bytes(b"replacement-sentinel")
                owner_record.write_bytes(b"replacement-owner")
                failure_record.write_bytes(b"replacement-failure")
                replacement = sentinel, owner_record, failure_record
            real_link(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        monkeypatch.setattr(os, "link", publish_after_swap)

    if os.name == "nt":
        process = _start(tmp_path, "--exit-while-pending")
        _expect_active_generation_exit(process)
        process.process.stdin.close()
        _wait(process)
        assert replacement is None
        assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
        process.close(time.monotonic() + 5)
    else:
        with pytest.raises(RuntimeError, match="identity changed"):
            _start(tmp_path, "--sleep-seconds", "30")
        assert replacement is not None
        sentinel, owner_record, failure_record = replacement
        assert sentinel.read_bytes() == b"replacement-sentinel"
        assert owner_record.read_bytes() == b"replacement-owner"
        assert failure_record.read_bytes() == b"replacement-failure"
        assert json.loads((moved / "owner.json").read_bytes())["state"] == "process_running"
        assert json.loads((moved / "failure.json").read_bytes())["code"] == "startup_failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle boundary required")
def test_repeated_startup_failures_close_every_returned_windows_handle_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = windows_workspace
    active: set[int] = set()
    opened: set[int] = set()
    children: list[subprocess.Popen[bytes]] = []
    real_close = workspace.close_handle
    real_popen = subprocess.Popen

    def track(name: str) -> None:
        real = getattr(workspace, name)

        def wrapped(*args: object, **kwargs: object) -> int:
            handle = real(*args, **kwargs)
            assert handle not in active
            active.add(handle)
            opened.add(handle)
            return handle

        monkeypatch.setattr(workspace, name, wrapped)

    def close(handle: int) -> None:
        real_close(handle)
        active.discard(handle)

    for operation in (
        "open_directory_path",
        "create_writable_directory",
        "create_directory",
        "create_file",
    ):
        track(operation)
    monkeypatch.setattr(workspace, "close_handle", close)

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    for index in range(5):
        nonce = f"{index + 1:032x}"
        with pytest.raises(RuntimeError, match="protocol startup failed"):
            LspProcess.start(
                _command("--sleep-seconds", "30"),
                cwd=tmp_path,
                owner_root=tmp_path / nonce,
            )
        assert not active
    assert opened
    assert children and _all_children_reaped(children)
    assert not _threads_for_nonces({f"{index + 1:032x}" for index in range(5)})


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle stress")
def test_windows_500_start_close_cycles_keep_process_handle_count_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gc

    monkeypatch.setattr(
        lsp_process_owner,
        "secure_windows_owner_root",
        lambda _path, _deadline: None,
    )
    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "sync_directory",
        lambda _owner: None,
    )

    def start_untracked() -> LspProcess:
        # Five hundred spawns on a hosted Windows image; the two-second default
        # startup budget is not what this test measures, and one cycle that
        # loses the race reports a thread-start timeout instead of a handle
        # leak. The path is otherwise the plain one.
        return lsp_process_startup.start_lsp_process_impl(
            LspProcess,
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
            configured_deadline=time.monotonic() + 30,
            generation_configuration=lsp_process_state.unconfigured_generation(),
        )

    retained_handles: list[object] = []
    warm = start_untracked()
    warm.close(time.monotonic() + 5)
    retained_handles.append(warm.process._handle)
    del warm
    gc.collect()
    baseline = _windows_process_handle_count()

    for _index in range(500):
        process = start_untracked()
        process.close(time.monotonic() + 5)
        retained_handles.append(process.process._handle)
        del process

    gc.collect()
    assert _windows_process_handle_count() <= baseline + 8
    assert all(getattr(handle, "closed", False) for handle in retained_handles)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job restart stress")
def test_windows_200_crash_restarts_with_children_have_no_false_failure_or_leaks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import ctypes
    import gc
    from ctypes import wintypes

    monkeypatch.setattr(
        lsp_process_owner,
        "secure_windows_owner_root",
        lambda _path, _deadline: None,
    )
    monkeypatch.setattr(
        lsp_process_owner.OwnerDirectory,
        "sync_directory",
        lambda _owner: None,
    )
    pid_log = tmp_path / "restart-descendants.txt"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    def wait_for_pid_line(index: int) -> int:
        # 200 restart cycles on a hosted Windows image; the child needs longer
        # than two seconds to record its pid when the runner is loaded.
        deadline = time.monotonic() + 30
        while True:
            lines = (
                pid_log.read_text(encoding="ascii").splitlines()
                if pid_log.exists()
                else []
            )
            if len(lines) > index:
                return int(lines[index])
            if time.monotonic() >= deadline:
                pytest.fail("timed out waiting for descendant identity")
            time.sleep(0.01)

    def open_identity(pid: int) -> int:
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def run_cycle(index: int) -> None:
        marker = tmp_path / f"crash-{index}.marker"
        before = (
            pid_log.read_text(encoding="ascii").splitlines() if pid_log.exists() else []
        )
        # The two-second default startup budget is not what two hundred crash
        # and restart cycles measure, and one cycle that loses it reports a
        # startup timeout instead of a leaked handle or a false failure. That
        # holds for the replacement too: without a bootstrap timeout it gets
        # the two-second default, and run 36083431199 lost one there.
        process = lsp_process_startup.start_lsp_process_impl(
            LspProcess,
            _command(
                "--lifecycle",
                "--spawn-descendant",
                "--descendant-pid-log",
                str(pid_log),
                "--crash-once-marker",
                str(marker),
            ),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
            configured_deadline=time.monotonic() + 30,
            generation_configuration=dataclasses.replace(
                lsp_process_state.unconfigured_generation(), bootstrap_timeout_seconds=30.0
            ),
        )
        handles: list[int] = []
        exit_results: list[int] = []
        try:
            handles.append(open_identity(wait_for_pid_line(len(before))))
            assert process.request(
                "echo", {"cycle": index}, deadline=time.monotonic() + SHORT_TIMEOUT
            ) == {"cycle": index}
            handles.append(open_identity(wait_for_pid_line(len(before) + 1)))
            assert process.restart_count == 1
            assert process.state is not ProcessState.FAILED
            assert not (process.owner_root / "failure.json").exists()
        finally:
            try:
                process.close(time.monotonic() + 10)
            finally:
                _wait_and_close_process_handles(kernel32, handles, exit_results)

        created = pid_log.read_text(encoding="ascii").splitlines()[len(before) :]
        assert len(created) == 2
        assert exit_results == [0, 0]
        assert not process.owner_root.exists()
        assert not lsp_process_state.coordinator_has_ownership(process._coordinator)

    run_cycle(-1)
    gc.collect()
    baseline = _windows_process_handle_count()

    for index in range(200):
        run_cycle(index)

    gc.collect()
    assert _windows_process_handle_count() <= baseline + 8


@pytest.mark.skipif(os.name != "nt", reason="Windows handle boundary required")
def test_owner_directory_close_is_exactly_once_with_mocked_windows_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(
        windows_workspace, "close_handle", lambda handle: closed.append(handle)
    )
    owner = lsp_process_owner.OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=(1, b"parent", True),
        owner_handle=202,
        owner_identity=(1, b"owner", True),
    )

    owner.close()
    owner.close()

    assert closed == [202, 101]


@pytest.mark.skipif(os.name != "nt", reason="Windows scratch retry boundary")
def test_windows_scratch_delete_open_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner_directory = coordinator.owner_directory
    assert owner_directory is not None
    real_open = windows_workspace.open_deletable_directory
    fail_owner_open = True

    def open_deletable_directory(parent: int, name: str) -> int:
        if parent == owner_directory.parent_handle and name == process.owner_root.name:
            if fail_owner_open:
                raise OSError("owner delete open failed")
        return real_open(parent, name)

    monkeypatch.setattr(
        windows_workspace,
        "open_deletable_directory",
        open_deletable_directory,
    )

    with pytest.raises(OSError, match="owner delete open failed"):
        process.shutdown(time.monotonic() + 5)

    assert process.owner_root.is_dir()
    assert owner_directory.owner_handle is None
    assert owner_directory._closed is False
    assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING

    fail_owner_open = False
    process.shutdown(time.monotonic() + 5)
    assert not process.owner_root.exists()


def test_posix_scratch_parent_sync_failure_is_retryable_after_directory_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PosixOperations:
        @staticmethod
        def listdir(_descriptor: int) -> list[str]:
            return []

        name = "posix"

        def __init__(self) -> None:
            self.owner_exists = True
            self.parent_syncs = 0

        @staticmethod
        def unlink(_name: str, *, dir_fd: int) -> None:
            assert dir_fd == 202
            raise FileNotFoundError

        def rmdir(self, name: str, *, dir_fd: int) -> None:
            if dir_fd == 202:
                assert name == "cancellation"
                raise FileNotFoundError
            assert dir_fd == 101 and name == OWNER_NONCE
            if not self.owner_exists:
                raise FileNotFoundError
            self.owner_exists = False

        def fsync(self, descriptor: int) -> None:
            if descriptor == 101:
                self.parent_syncs += 1
                if self.parent_syncs == 1:
                    raise OSError("parent sync failed")

        def stat(
            self,
            name: str,
            *,
            dir_fd: int,
            follow_symlinks: bool,
        ) -> object:
            assert name == OWNER_NONCE
            assert dir_fd == 101
            assert follow_symlinks is False
            if not self.owner_exists:
                raise FileNotFoundError
            return type(
                "OwnerInfo",
                (),
                {
                    "st_dev": 1,
                    "st_ino": 2,
                    "st_mode": stat.S_IFDIR | 0o700,
                    "st_file_attributes": 0,
                },
            )()

    operations = PosixOperations()
    owner = lsp_process_owner.OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=object(),
        owner_handle=202,
        owner_identity=lsp_process_owner.ObjectIdentity(1, 2, stat.S_IFDIR, 0),
    )
    monkeypatch.setattr(lsp_process_owner, "os", operations)

    with pytest.raises(OSError, match="parent sync failed"):
        owner.remove_success_scratch()

    owner.remove_success_scratch()
    assert operations.owner_exists is False
    assert operations.parent_syncs == 2


def test_posix_success_scratch_never_deletes_replacement_owner_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    replacement_deleted = False

    class PosixOperations:
        @staticmethod
        def listdir(_descriptor: int) -> list[str]:
            return []

        name = "posix"

        @staticmethod
        def unlink(_name: str, *, dir_fd: int) -> None:
            assert dir_fd == 202
            raise FileNotFoundError

        @staticmethod
        def fsync(_descriptor: int) -> None:
            return None

        @staticmethod
        def stat(
            name: str,
            *,
            dir_fd: int,
            follow_symlinks: bool,
        ) -> object:
            assert name == OWNER_NONCE
            assert dir_fd == 101
            assert follow_symlinks is False
            return type(
                "ReplacementInfo",
                (),
                {
                    "st_dev": 1,
                    "st_ino": 3,
                    "st_mode": stat.S_IFDIR | 0o700,
                    "st_file_attributes": 0,
                },
            )()

        @staticmethod
        def rmdir(name: str, *, dir_fd: int) -> None:
            nonlocal replacement_deleted
            if dir_fd == 202:
                assert name == "cancellation"
                raise FileNotFoundError
            replacement_deleted = True

    owner = lsp_process_owner.OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=object(),
        owner_handle=202,
        owner_identity=lsp_process_owner.ObjectIdentity(1, 2, stat.S_IFDIR, 0),
    )
    monkeypatch.setattr(lsp_process_owner, "os", PosixOperations())

    with pytest.raises(PermissionError, match="identity changed"):
        owner.remove_success_scratch()

    assert replacement_deleted is False


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="Linux descriptor accounting required",
)
def test_repeated_startup_failures_do_not_leak_posix_descriptors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    children: list[subprocess.Popen[bytes]] = []
    initial_threads = _lsp_thread_names()
    real_popen = subprocess.Popen

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    for index in range(5):
        with pytest.raises(RuntimeError, match="protocol startup failed"):
            LspProcess.start(
                _command("--sleep-seconds", "30"),
                cwd=tmp_path,
                owner_root=tmp_path / f"{index + 1:032x}",
            )
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before
    assert children and all(child.poll() is not None for child in children)
    assert _lsp_thread_names() == initial_threads


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited ACL contract")
def test_windows_owner_acl_uses_one_bounded_change_and_one_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    identity = compile_cache.windows_acl_identity()
    commands: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append((command, kwargs))
        output = f"{owner} {identity}:(OI)(CI)(F)\n".encode()
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen failed")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", fail_popen)

    with pytest.raises(OSError, match="popen failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    broad = list(lsp_process_owner.BROAD_ACL_SIDS)
    assert [command for command, _kwargs in commands] == [
        [
            "icacls",
            str(owner),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(OI)(CI)(F)",
        ],
        ["icacls", str(owner), "/remove:g", *broad],
        ["icacls", str(owner), "/remove:d", *broad],
        ["icacls", str(owner)],
    ]
    for _command_args, kwargs in commands:
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        # The budget is a deadline minus a later clock read, so rounding can
        # leave it an ulp above the constant it was derived from.
        assert 0 < float(kwargs["timeout"]) <= lsp_process_limits.STARTUP_WAIT_SECONDS + 1e-6
    assert set(path.name for path in owner.iterdir()) == {
        "cancellation",
        "failure.json",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows cleanup contention contract")
def test_parallel_real_startup_failures_stay_bounded_secure_and_leak_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    children_lock = threading.Lock()
    real_popen = subprocess.Popen
    initial_threads = _lsp_thread_names()

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        with children_lock:
            children.append(child)
        return child

    def fail_start(index: int) -> tuple[float, Path]:
        owner = tmp_path / f"{index + 100:032x}"
        started = time.monotonic()
        with pytest.raises(RuntimeError) as raised:
            LspProcess.start(
                _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
            )
        elapsed = time.monotonic() - started
        _assert_protocol_startup_failure(raised.value)
        return elapsed, owner

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process_generation, "LspProtocol", BrokenProtocol)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fail_start, range(20)))

    assert max(elapsed for elapsed, _owner in results) <= (
        _STARTUP_CEILING_SECONDS
    )
    assert len(children) == 20
    assert all(child.poll() is not None for child in children)
    for _elapsed, owner in results:
        _assert_startup_failure_evidence(owner, tmp_path)

    assert _await_thread_names(initial_threads) == initial_threads


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL subprocess contract")
def test_four_delayed_owner_acl_starts_share_deadline_and_leave_no_leaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initial_threads = _lsp_thread_names()

    def delayed_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        timeout = float(kwargs["timeout"])
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    def fail_start(index: int) -> tuple[float, Path]:
        owner = tmp_path / f"{index + 200:032x}"
        started = time.monotonic()
        with pytest.raises(
            (PermissionError, lsp_process_cleanup.StartupCleanupError)
        ) as raised:
            LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
        elapsed = time.monotonic() - started
        if isinstance(raised.value, lsp_process_cleanup.StartupCleanupError):
            raised.value.retry_cleanup(time.monotonic() + 5)
        return elapsed, owner

    monkeypatch.setattr(subprocess, "run", delayed_run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fail_start, range(4)))

    assert max(elapsed for elapsed, _owner in results) <= (
        _STARTUP_CEILING_SECONDS
    )
    assert all(owner.is_dir() and not any(owner.iterdir()) for _elapsed, owner in results)
    for _elapsed, owner in results:
        owner.rmdir()
    assert _lsp_thread_names() == initial_threads
