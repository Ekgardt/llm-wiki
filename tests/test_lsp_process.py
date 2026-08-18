"""Process ownership and bounded transport tests for LSP children."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import json
import math
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import compile_cache
import lsp_process
import lsp_protocol
import pytest
from lsp_process import (
    LSP_ENV_ALLOWLIST,
    MAX_STDERR_BYTES,
    LspProcess,
    ProcessState,
    lsp_environment,
)
from lsp_protocol import (
    MAX_FRAME_BYTES,
    CancellationSource,
    JsonRpcResponseError,
    ProtocolViolation,
    RequestCancelled,
)

FAKE_SERVER = Path(__file__).with_name("fake_lsp_server.py").resolve()
OWNER_NONCE = "a" * 32


def _await_records(records: list, count: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while len(records) < count and time.monotonic() < deadline:
        time.sleep(0.01)


def _all_deadlines_ahead(attempts) -> bool:
    return all(deadline > started for started, deadline in attempts)


def _capture_start_error(returned: list, tmp_path: Path) -> BaseException | None:
    try:
        returned.append(_start(tmp_path, "--lifecycle", "--sleep-seconds", "30"))
    except BaseException as raised:  # noqa: BLE001 - the failure is the subject
        return raised
    return None


def _await_process_exit(child, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)


def _retry_owned_cleanup(coordinator, cleanup_error) -> None:
    """Finish whatever cleanup this coordinator still owns, by either route."""
    if coordinator is None or not lsp_process._coordinator_has_ownership(coordinator):
        return
    if cleanup_error is not None:
        cleanup_error.retry_cleanup(time.monotonic() + 5)
        return
    lsp_process._retry_startup_cleanup(coordinator, time.monotonic() + 5)


def _cause_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None:
        chain.append(current)
        current = current.__cause__
    return chain


def _drive_retained_cleanup(error: BaseException | None) -> None:
    """Finish cleanup for a startup that returned ownership with its error."""
    if not isinstance(error, lsp_process.StartupCleanupError):
        return
    coordinator = error.coordinator
    assert coordinator is not None
    lsp_process._drive_cleanup(
        None,
        time.monotonic() + 5,
        terminal=True,
        failure_code="startup_failed",
        coordinator_override=coordinator,
    )


def _expect_blocked_lease_write(coordinator, owner) -> None:
    """Writing the lease keeps failing while its temporary cannot be removed."""
    lsp_process._acquire_lease(coordinator, time.monotonic() + 1)
    try:
        with pytest.raises(OSError, match="lease temporary deletion failed"):
            owner.write_lease({"state": "live"})
    finally:
        lsp_process._release_lease(coordinator)


def _entry_names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def _expect_repeated_close_failure(process, message: str, attempts: int = 2) -> None:
    """The same failure must repeat until the blocked cleanup is released."""
    for _attempt in range(attempts):
        with pytest.raises(OSError, match=message):
            process.close(time.monotonic() + 5)


def _await_children_reaped(children, deadline: float) -> None:
    while not _all_children_reaped(children) and time.monotonic() < deadline:
        time.sleep(0.01)


def _is_blocked_temporary(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and name.endswith(".tmp")


def _refuse_until_allowed(allowed, message: str) -> None:
    if not allowed.is_set():
        raise OSError(message)


def _cleanup_error_steps(coordinator) -> set[tuple[str, str]]:
    return {(item.step, item.error_type) for item in coordinator.cleanup_result.errors}


def _remove_temporaries(owner_root: Path, pattern: str) -> None:
    for temporary in owner_root.glob(pattern):
        temporary.unlink()


def _assert_protocol_startup_failure(error: BaseException) -> None:
    """Either the plain protocol failure, or one wrapping it with retryable cleanup."""
    if isinstance(error, lsp_process.StartupCleanupError):
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


def _mutate_failure_record(record: dict, mutation: str, pid: int) -> None:
    """One field of a terminal failure record that must break its identity."""
    mutations = {
        "wrong-owner": lambda: record.__setitem__("owner_nonce", "b" * 32),
        "wrong-generation": lambda: record.__setitem__("generation_nonce", "b" * 32),
        "wrong-code": lambda: record.__setitem__("code", "process_exited"),
        "missing-pid": lambda: record.pop("server_pid"),
        "wrong-pid": lambda: record.__setitem__("server_pid", pid + 1),
        "noncanonical-timestamp": lambda: record.__setitem__(
            "timestamp", "2026-07-24T12:34:56.1Z"
        ),
    }
    mutations[mutation]()


def _lsp_thread_names() -> set[str]:
    return {thread.name for thread in _lsp_threads()}


def _all_children_reaped(children) -> bool:
    return all(child.poll() is not None for child in children)


def _threads_for_nonces(nonces: set[str]) -> list[str]:
    """LSP threads still named after one of these owner nonces."""
    return [
        thread.name
        for thread in _lsp_threads()
        if any(nonce in thread.name for nonce in nonces)
    ]


def _optional_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_bytes())


def _assert_owner_state_for_stage(owner_record, failure_stage: str) -> None:
    """An owner record exists only once the process itself was running."""
    if failure_stage != "protocol":
        assert owner_record is None
        return
    assert owner_record is not None
    assert owner_record["state"] == "process_running"


def _assert_evidence_is_sanitized(owner: Path, owner_record, tmp_path: Path) -> None:
    evidence = (owner / "failure.json").read_text()
    if owner_record is not None:
        evidence += (owner / "owner.json").read_text()
    assert "repository-secret" not in evidence
    assert str(tmp_path) not in evidence


def _assert_failure_evidence_is_private(owner: Path, owner_record) -> None:
    if os.name == "posix":
        assert stat.S_IMODE((owner / "failure.json").stat().st_mode) == 0o600
    _assert_lsp_acl_is_owner_only(owner, inherited=False)
    _assert_lsp_acl_is_owner_only(owner / "cancellation", inherited=os.name == "nt")
    if owner_record is not None:
        _assert_lsp_acl_is_owner_only(owner / "owner.json", inherited=os.name == "nt")
    _assert_lsp_acl_is_owner_only(owner / "failure.json", inherited=os.name == "nt")


def _cleanup_error_types(coordinator) -> set[str]:
    return {item.error_type for item in coordinator.cleanup_result.errors}


def _generations_with_process(coordinator) -> list:
    """Generations that still hold a process — none may survive cleanup."""
    generations = [coordinator.active, coordinator.candidate, *coordinator.retired]
    return [
        generation
        for generation in generations
        if generation is not None and generation.process is not None
    ]


def _stop_heartbeat_thread(coordinator) -> None:
    coordinator.heartbeat_stop.set()
    coordinator.heartbeat_wake.set()
    heartbeat = coordinator.heartbeat_thread
    if heartbeat is not None:
        heartbeat.join(1)


def _release_owner_directory(coordinator) -> None:
    owner_directory = coordinator.owner_directory
    if owner_directory is None:
        return
    try:
        owner_directory.remove_lease()
        owner_directory.close()
    except OSError:
        pass


def _release_captured_coordinator(coordinator) -> None:
    _stop_heartbeat_thread(coordinator)
    _release_owner_directory(coordinator)
    lsp_process._unregister_startup_cleanup(coordinator)


def _owner_entry_names(process) -> set[str]:
    return {path.name for path in process.owner_root.iterdir()}


def _join_started(thread, timeout: float = 5) -> None:
    if thread.ident is not None:
        thread.join(timeout)


def _close_if_owned(process, coordinator) -> None:
    if lsp_process._coordinator_has_ownership(coordinator):
        process.close(time.monotonic() + 5)


def _all_messages_equal(errors, message: str) -> bool:
    return all(str(error) == message for error in errors)


def _owned_coordinators(coordinators) -> list:
    return [
        coordinator
        for coordinator in coordinators
        if lsp_process._coordinator_has_ownership(coordinator)
    ]


def _unregister_all(coordinators) -> None:
    for coordinator in coordinators:
        lsp_process._unregister_startup_cleanup(coordinator)


def _close_owned_process(process, coordinator) -> None:
    """Close while ownership remains, retrying once past a transient refusal."""
    if not lsp_process._coordinator_has_ownership(coordinator):
        return
    try:
        process.close(time.monotonic() + 5)
    except RuntimeError:
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def _unregister_new_cleanup_owners(baseline: set[int]) -> None:
    for coordinator in lsp_process._pending_startup_cleanup_snapshot():
        if id(coordinator) not in baseline:
            lsp_process._unregister_startup_cleanup(coordinator)


def _await_pending_request(process, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while process.protocol.pending_count == 0 and time.monotonic() < deadline:
        time.sleep(0.001)


def _is_replacement_process(current: object, bootstraps) -> bool:
    """True for the second generation's tree, once it exists."""
    if len(bootstraps) <= 1:
        return False
    return current.process.pid == bootstraps[1][2]  # type: ignore[attr-defined]


def _await_release(release, deadline: float, message: str) -> None:
    if not release.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError(message)


_GUARD_STAGES = ("factory", "enter", "spawn", "bootstrap", "exit")


def _assert_guard_stages(events, deadlines, nonce: str) -> None:
    """One generation passes every stage once, under a single deadline."""
    assert [stage for stage, current in events if current == nonce] == list(
        _GUARD_STAGES
    )
    assert set(deadlines[nonce]) == set(_GUARD_STAGES)
    assert len(set(deadlines[nonce].values())) == 1


def _retry_cleanup_errors(errors) -> None:
    for error in errors:
        if isinstance(error, lsp_process.StartupCleanupError):
            error.retry_cleanup(time.monotonic() + 5)


def _close_owned(processes) -> None:
    for process in processes:
        if lsp_process._coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)


def _pending_cleanup_ids() -> set[int]:
    return {id(item) for item in lsp_process._pending_startup_cleanup_snapshot()}


def _collect_start_failure(failures: list, tmp_path: Path, nonce: int) -> None:
    try:
        LspProcess.start(
            _command(), cwd=tmp_path, owner_root=tmp_path / f"{nonce:032x}"
        )
    except BaseException as error:  # noqa: BLE001 - the failure is the subject
        failures.append(error)


def _await_restart(process, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while process.restart_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)


def _failure_settled(process) -> bool:
    """Failed, with no recovery thread still working on the ending."""
    if process.state is not ProcessState.FAILED:
        return False
    recovery = process._recovery_thread
    return recovery is None or not recovery.is_alive()


def _await_settled_failure(process, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not _failure_settled(process) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is ProcessState.FAILED


def _await_descendant_pid(pid_file: Path, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return int(pid_file.read_text(encoding="ascii"))


def _await_pid_exit(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)


def _await_restart_with_lease(process, lease_path: Path, deadline: float) -> None:
    """Wait for the replacement generation to publish its lease."""
    while time.monotonic() < deadline:
        if process.restart_count > 0 and lease_path.exists():
            return
        time.sleep(0.01)


def _reraise_unless_windows_retry(deadline: float) -> None:
    """Windows may deny a lease read while it is replaced; retry until deadline."""
    if os.name != "nt" or time.monotonic() >= deadline:
        raise
    time.sleep(0.01)


def _expected_windows_denials() -> int:
    return 1 if os.name == "nt" else 0


def _assert_distinct_sanitized_failures(errors) -> None:
    """Every waiter gets its own error object with the same public message."""
    assert len({id(error) for error in errors}) == len(errors)
    assert all(isinstance(error, ProtocolViolation) for error in errors)
    assert [str(error) for error in errors] == ["LSP replacement startup failed"] * len(
        errors
    )
    assert all(error.__suppress_context__ for error in errors)


def _assert_single_cause(errors) -> list:
    """Exactly one waiter carries the original cause; the rest carry none."""
    causes = [error.__cause__ for error in errors]
    assert sum(cause is not None for cause in causes) == 1
    cause = next(cause for cause in causes if cause is not None)
    assert isinstance(cause, RuntimeError)
    assert str(cause) == "LSP replacement startup cause (RuntimeError)"
    return causes


def _await_lease_refresh(read_lease, lease_exists: bool):
    """Wait for one heartbeat to move the lease record, bounded to a second."""
    if not lease_exists:
        return {}, False
    record = read_lease(time.monotonic() + 1)
    first_heartbeat = record["heartbeat_at"]
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        current = read_lease(deadline)
        if current["heartbeat_at"] != first_heartbeat:
            return current, True
        time.sleep(0.01)
    return record, False


def _nonce_of(generation) -> str | None:
    return generation.nonce if generation is not None else None


def _transition_snapshot(coordinator):
    with coordinator.condition:
        return (
            coordinator.phase,
            coordinator.active,
            _nonce_of(coordinator.candidate),
            _nonce_of(coordinator.lease_generation),
            coordinator.startup_complete,
        )


def _lsp_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith("lsp-")]


def _lsp_thread_ids() -> set[int]:
    return {id(thread) for thread in _lsp_threads()}


def _leaked_lsp_threads(existing: set[int]) -> list[str]:
    return [thread.name for thread in _lsp_threads() if id(thread) not in existing]


@pytest.fixture(autouse=True)
def _no_lsp_lifecycle_owner_leaks(
    monkeypatch: pytest.MonkeyPatch,
):
    started: list[LspProcess] = []
    start = LspProcess.start.__func__
    configured_start = LspProcess.start_configured.__func__

    def tracked_start(
        cls: type[LspProcess],
        command: object,
        *,
        cwd: Path,
        owner_root: Path,
    ) -> LspProcess:
        process = start(cls, command, cwd=cwd, owner_root=owner_root)
        started.append(process)
        return process

    monkeypatch.setattr(LspProcess, "start", classmethod(tracked_start))

    def tracked_configured_start(
        cls: type[LspProcess],
        command: object,
        *,
        cwd: Path,
        owner_root: Path,
        deadline: float,
        server_request_handlers: object,
        server_notification_handlers: object,
        generation_bootstrap: object,
        bootstrap_timeout_seconds: float | None = None,
        generation_guard: object | None = None,
    ) -> LspProcess:
        options = {}
        if generation_guard is not None:
            options["generation_guard"] = generation_guard
        process = configured_start(
            cls,
            command,
            cwd=cwd,
            owner_root=owner_root,
            deadline=deadline,
            server_request_handlers=server_request_handlers,
            server_notification_handlers=server_notification_handlers,
            generation_bootstrap=generation_bootstrap,
            bootstrap_timeout_seconds=bootstrap_timeout_seconds,
            **options,
        )
        started.append(process)
        return process

    monkeypatch.setattr(
        LspProcess, "start_configured", classmethod(tracked_configured_start)
    )
    existing = _lsp_thread_ids()
    yield
    owned = [
        process.owner_nonce
        for process in started
        if lsp_process._coordinator_has_ownership(process._coordinator)
    ]
    assert owned == []
    assert _leaked_lsp_threads(existing) == []


def _command(*arguments: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *arguments]


def _hold_protocol_constructor_until_callback(
    monkeypatch: pytest.MonkeyPatch,
    callback_finished: threading.Event,
) -> None:
    protocol_type = lsp_process.LspProtocol

    class ConstructorCallbackProbe(protocol_type):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            if not callback_finished.wait(2):
                raise TimeoutError("startup callback did not run before constructor return")

    monkeypatch.setattr(lsp_process, "LspProtocol", ConstructorCallbackProbe)


def _start(tmp_path: Path, *arguments: str) -> LspProcess:
    return LspProcess.start(
        _command(*arguments), cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE
    )


def _initialize_generation(
    protocol: lsp_protocol.LspProtocol,
    _pid: int,
    _generation_nonce: str,
    deadline: float,
) -> ProcessState:
    assert protocol.request("initialize", {}, deadline=deadline) == {"capabilities": {}}
    protocol.notify("initialized", {}, deadline=deadline)
    return ProcessState.PROTOCOL_INITIALIZED


def _wait(process: LspProcess, seconds: float = 10) -> int:
    return process.wait_for_exit(time.monotonic() + seconds)


def _expect_active_generation_exit(process: LspProcess) -> None:
    generation = process._coordinator.active
    assert generation is not None
    assert lsp_process._mark_generation_expected_exit(generation)


class _FakeTree:
    def __init__(self, process: object) -> None:
        self.process = process

    def terminate(self, *, deadline: float) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=max(0.0, (deadline - time.monotonic()) / 2))
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("fake tree stayed alive") from exc

    def close(self) -> None:
        if self.process.poll() is None:
            raise RuntimeError("fake tree stayed live")


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _posix_pid_alive(pid: int) -> bool:
    """EPERM means the process exists under another user, not that it is gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def _windows_handle_status(handle: int) -> tuple[bool, int]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetHandleInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetHandleInformation.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    ctypes.set_last_error(0)
    valid = bool(kernel32.GetHandleInformation(handle, ctypes.byref(flags)))
    return valid, ctypes.get_last_error()


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


def _wait_for_owned_pids_to_exit(
    process: LspProcess,
    pids: list[int],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        live = _live_pids(pids)
        if not live:
            return
        _assert_still_owned(process, live)
        _fail_past_deadline(deadline, live)
        time.sleep(0.01)


def _live_pids(pids: list[int]) -> list[int]:
    return [pid for pid in pids if _pid_alive(pid)]


def _fail_past_deadline(deadline: float, live: list[int]) -> None:
    if time.monotonic() >= deadline:
        pytest.fail(f"owned LSP PIDs did not exit before deadline: {live}")


def _assert_still_owned(process: LspProcess, live: list[int]) -> None:
    """While owned PIDs remain, the tree, owner and lease must all be held."""
    coordinator = process._coordinator
    with coordinator.condition:
        generations = lsp_process._generations_locked(coordinator)
        assert any(generation.tree is not None for generation in generations), live
        owner = coordinator.owner_directory
        assert owner is not None and not owner._closed, live
        assert (process.owner_root / "lease.json").is_file(), live


def _assert_lsp_acl_is_owner_only(path: Path, *, inherited: bool) -> None:
    if os.name != "nt":
        compile_cache._verify_owner_only(path, 0o700 if path.is_dir() else 0o600)
        return
    acl = compile_cache._acl_output_text(
        compile_cache._run_acl_command(["icacls", str(path)]).stdout
    )
    acl_lines = [line.strip() for line in acl.splitlines() if ":(" in line]
    assert len(acl_lines) == 1
    assert compile_cache._acl_principal(path, acl_lines[0]).casefold() == (
        compile_cache._windows_acl_identity().casefold()
    )
    assert "(F)" in acl_lines[0]
    assert ("(I)" in acl_lines[0]) is inherited


def test_constants_states_and_public_dataclass_fields_are_exact() -> None:
    assert MAX_STDERR_BYTES == 4 * 1024 * 1024
    assert LSP_ENV_ALLOWLIST == frozenset(
        {
            "COMSPEC",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "USERPROFILE",
            "WINDIR",
        }
    )
    assert [(state.name, state.value) for state in ProcessState] == [
        ("PROCESS_RUNNING", "process_running"),
        ("PROTOCOL_INITIALIZED", "protocol_initialized"),
        ("WORKSPACE_READY", "workspace_ready"),
        ("DEGRADED", "degraded"),
        ("FAILED", "failed"),
    ]
    fields = dataclasses.fields(LspProcess)
    public = {field.name for field in fields if not field.name.startswith("_")}
    assert public == {
        "process",
        "protocol",
        "owner_root",
        "owner_nonce",
        "generation_nonce",
        "state",
        "started_monotonic",
        "last_used_monotonic",
        "restart_count",
    }
    assert LspProcess.__slots__ == tuple(field.name for field in fields)
    assert not hasattr(object.__new__(LspProcess), "__dict__")


def test_configured_start_installs_handlers_before_bootstrap_and_uses_caller_deadline(
    tmp_path: Path,
) -> None:
    configurations: list[object] = []
    progress: list[object] = []
    bootstraps: list[tuple[int, str, float]] = []
    deadline = time.monotonic() + 5

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        received_deadline: float,
    ) -> ProcessState:
        bootstraps.append((pid, generation_nonce, received_deadline))
        return _initialize_generation(protocol, pid, generation_nonce, received_deadline)

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=deadline,
        server_request_handlers={
            "workspace/configuration": lambda params: configurations.append(params) or True
        },
        server_notification_handlers={"$/progress": progress.append},
        generation_bootstrap=bootstrap,
    )
    try:
        assert configurations == [{"items": [{"section": "python"}]}]
        assert progress == [{"token": "bootstrap", "value": {"kind": "begin"}}]
        assert bootstraps == [(process.process.pid, process.generation_nonce, deadline)]
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        process.close(time.monotonic() + 5)


def test_initial_bootstrap_publishes_candidate_lease_and_refreshes_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_HEARTBEAT_SECONDS", 0.05)
    coordinators: list[lsp_process._LifecycleCoordinator] = []
    real_prepare = lsp_process._prepare_generation
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    bootstrap_nonces: list[str] = []
    started: list[LspProcess] = []
    start_errors: list[BaseException] = []

    def observe_prepare(
        coordinator: lsp_process._LifecycleCoordinator,
        *args: object,
        **kwargs: object,
    ) -> lsp_process._Generation:
        coordinators.append(coordinator)
        return real_prepare(coordinator, *args, **kwargs)  # type: ignore[arg-type]

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstrap_nonces.append(generation_nonce)
        bootstrap_entered.set()
        assert release_bootstrap.wait(max(0.0, deadline - time.monotonic()))
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    def start() -> None:
        try:
            started.append(
                LspProcess.start_configured(
                    _command("--lifecycle", "--bootstrap-handshake"),
                    cwd=tmp_path,
                    owner_root=tmp_path / OWNER_NONCE,
                    deadline=time.monotonic() + 3,
                    server_request_handlers={
                        "workspace/configuration": lambda _params: True
                    },
                    server_notification_handlers={"$/progress": lambda _params: None},
                    generation_bootstrap=bootstrap,
                )
            )
        except BaseException as error:
            start_errors.append(error)

    monkeypatch.setattr(lsp_process, "_prepare_generation", observe_prepare)
    owner_root = tmp_path / OWNER_NONCE
    lease_path = owner_root / "lease.json"
    path_read_bytes = Path.read_bytes
    transient_read_denials = 0

    def deny_one_concurrent_lease_read(path: Path) -> bytes:
        nonlocal transient_read_denials
        if path == lease_path and transient_read_denials == 0:
            transient_read_denials += 1
            raise PermissionError("simulated concurrent Windows lease replacement")
        return path_read_bytes(path)

    def read_lease(deadline: float) -> dict[str, object]:
        while True:
            try:
                return json.loads(lease_path.read_bytes())
            except PermissionError:
                _reraise_unless_windows_retry(deadline)

    thread = threading.Thread(target=start)
    thread.start()
    try:
        assert bootstrap_entered.wait(2)
        coordinator = coordinators[0]
        owner_exists_during_bootstrap = (owner_root / "owner.json").is_file()
        lease_exists_during_bootstrap = lease_path.is_file()
        if os.name == "nt":
            monkeypatch.setattr(Path, "read_bytes", deny_one_concurrent_lease_read)
        lease_record, heartbeat_refreshed = _await_lease_refresh(
            read_lease, lease_exists_during_bootstrap
        )
        transition_snapshot = _transition_snapshot(coordinator)
        release_bootstrap.set()
        thread.join(5)
        assert not thread.is_alive()
        assert start_errors == []
        assert len(started) == 1
        assert owner_exists_during_bootstrap is True
        assert lease_exists_during_bootstrap is True
        assert transient_read_denials == _expected_windows_denials()
        assert heartbeat_refreshed is True
        assert lease_record["generation_nonce"] == bootstrap_nonces[0]
        assert transition_snapshot == (
            lsp_process._LifecyclePhase.STARTING,
            None,
            bootstrap_nonces[0],
            bootstrap_nonces[0],
            False,
        )
        assert started[0].state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        release_bootstrap.set()
        thread.join(5)
        _close_owned(started)
        _retry_cleanup_errors(start_errors)


def test_lsp_publication_uses_canonical_token_and_epoch_in_owner_and_lease_records(
    tmp_path: Path,
) -> None:
    import markdown_transaction

    state_root = tmp_path / "state"
    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    owner_root = state_root / "run/lsp" / OWNER_NONCE
    owner_root.parent.mkdir(parents=True)

    process = LspProcess._start_with_v3_candidate(
        _command("--lifecycle"),
        cwd=tmp_path,
        owner_root=owner_root,
        state_root=state_root,
    )
    try:
        owner = json.loads((owner_root / "owner.json").read_bytes())
        lease = json.loads((owner_root / "lease.json").read_bytes())
        with sqlite3.connect(candidate) as database:
            canonical = database.execute(
                "SELECT role, scope, actor_id, owner_token, fencing_epoch, "
                "process_start_identity FROM maintenance_owners WHERE role='lsp'"
            ).fetchone()
        expected = (
            owner["canonical_role"],
            owner["canonical_scope"],
            owner["actor_id"],
            owner["owner_token"],
            owner["fencing_epoch"],
            owner["process_start_identity"],
        )
        assert canonical == expected
        assert tuple(
            lease[field]
            for field in (
                "canonical_role",
                "canonical_scope",
                "actor_id",
                "owner_token",
                "fencing_epoch",
                "process_start_identity",
            )
        ) == expected
    finally:
        process.close(time.monotonic() + 5)

    assert not (owner_root / "lease.json").exists()
    with sqlite3.connect(candidate) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM maintenance_owners WHERE role='lsp'"
        ).fetchone() == (0,)


def test_startup_commit_rejects_heartbeat_terminal_failure_during_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_HEARTBEAT_SECONDS", 60.0)
    coordinators: list[lsp_process._LifecycleCoordinator] = []
    real_prepare = lsp_process._prepare_generation
    real_write_lease = lsp_process._OwnerDirectory.write_lease
    real_select_terminal = lsp_process._select_terminal_failure_locked
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    heartbeat_failed = threading.Event()
    terminal_recorded = threading.Event()
    write_lock = threading.Lock()
    lease_writes = 0
    started: list[LspProcess] = []
    start_errors: list[BaseException] = []

    def observe_prepare(
        coordinator: lsp_process._LifecycleCoordinator,
        *args: object,
        **kwargs: object,
    ) -> lsp_process._Generation:
        coordinators.append(coordinator)
        return real_prepare(coordinator, *args, **kwargs)  # type: ignore[arg-type]

    def fail_second_lease(
        owner: lsp_process._OwnerDirectory,
        record: object,
        **kwargs: object,
    ) -> None:
        nonlocal lease_writes
        with write_lock:
            lease_writes += 1
            current_write = lease_writes
        if current_write == 2:
            heartbeat_failed.set()
            raise OSError("injected startup heartbeat failure")
        real_write_lease(owner, record, **kwargs)  # type: ignore[arg-type]

    def observe_terminal(
        instance: LspProcess | None,
        coordinator: lsp_process._LifecycleCoordinator,
        code: str,
    ) -> bool:
        selected = real_select_terminal(instance, coordinator, code)
        if coordinators and coordinator is coordinators[0]:
            terminal_recorded.set()
        return selected

    def bootstrap(
        _protocol: lsp_protocol.LspProtocol,
        _pid: int,
        _generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstrap_entered.set()
        if not release_bootstrap.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("startup bootstrap release expired")
        return ProcessState.PROCESS_RUNNING

    def start() -> None:
        try:
            started.append(
                LspProcess.start_configured(
                    _command("--lifecycle"),
                    cwd=tmp_path,
                    owner_root=tmp_path / OWNER_NONCE,
                    deadline=time.monotonic() + 5,
                    server_request_handlers={},
                    server_notification_handlers={},
                    generation_bootstrap=bootstrap,
                )
            )
        except BaseException as error:
            start_errors.append(error)

    monkeypatch.setattr(lsp_process, "_prepare_generation", observe_prepare)
    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", fail_second_lease)
    monkeypatch.setattr(
        lsp_process,
        "_select_terminal_failure_locked",
        observe_terminal,
    )
    monkeypatch.setattr(lsp_process, "_start_lifecycle_workers", lambda *_args: None)
    owner_root = tmp_path / OWNER_NONCE
    thread = threading.Thread(target=start)
    thread.start()
    assert bootstrap_entered.wait(2)
    coordinator = coordinators[0]
    heartbeat = coordinator.heartbeat_thread
    assert heartbeat is not None
    coordinator.heartbeat_wake.set()
    assert heartbeat_failed.wait(1)
    assert terminal_recorded.wait(1)
    with coordinator.terminal_state_lock:
        assert coordinator.pending_failure_intents == 1
        assert coordinator.success_committed is False
    with coordinator.condition:
        assert coordinator.terminal_outcome == "failure"
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPING_FAILURE

    release_bootstrap.set()
    thread.join(5)
    try:
        assert not thread.is_alive()
        assert started == []
        assert len(start_errors) == 1
        assert coordinator.startup_complete is False
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
        assert not lsp_process._coordinator_has_ownership(coordinator)
        assert (owner_root / "failure.json").is_file()
        assert not (owner_root / "lease.json").exists()
    finally:
        release_bootstrap.set()
        thread.join(5)
        _close_owned(started)
        _retry_cleanup_errors(start_errors)


def test_transparent_restart_bootstraps_fresh_generation_before_request_replay(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "query-crashed"
    configurations: list[object] = []
    progress: list[object] = []
    bootstraps: list[tuple[int, str]] = []

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append((pid, generation_nonce))
        _initialize_generation(protocol, pid, generation_nonce, deadline)
        return (
            ProcessState.PROTOCOL_INITIALIZED
            if len(bootstraps) == 1
            else ProcessState.WORKSPACE_READY
        )

    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--query-crash-once-marker",
            str(marker),
            "--require-initialized-query",
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={
            "workspace/configuration": lambda params: configurations.append(params) or True
        },
        server_notification_handlers={"$/progress": progress.append},
        generation_bootstrap=bootstrap,
    )
    first_pid = process.process.pid
    first_nonce = process.generation_nonce
    try:
        assert process.state is ProcessState.PROTOCOL_INITIALIZED

        result = process.request(
            "initialized/query", {"retry": True}, deadline=time.monotonic() + 5
        )

        assert result["initialized"] is True
        assert process.restart_count == 1
        assert process.state is ProcessState.WORKSPACE_READY
        assert process.process.pid != first_pid
        assert process.generation_nonce != first_nonce
        assert bootstraps == [
            (first_pid, first_nonce),
            (process.process.pid, process.generation_nonce),
        ]
        assert len(configurations) == 2
        assert len(progress) == 2
    finally:
        process.close(time.monotonic() + 5)


def test_delayed_crash_restart_commits_with_sub_half_second_live_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "delayed-query-crashed"
    bootstraps: list[tuple[str, float]] = []
    protocol_deadlines: list[float] = []
    replacement_margins: list[float] = []
    fresh_deadlines: list[float] = []
    commit_deadlines: list[float] = []
    real_protocol = lsp_process.LspProtocol
    real_fresh_deadline = lsp_process._fresh_bootstrap_deadline
    real_commit = lsp_process._commit_restart_generation_owned

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append((generation_nonce, deadline))
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    def delayed_protocol(*args: object, **kwargs: object) -> lsp_protocol.LspProtocol:
        deadline = kwargs.get("_startup_deadline")
        assert isinstance(deadline, float)
        protocol_deadlines.append(deadline)
        if len(protocol_deadlines) == 2:
            delay = deadline - time.monotonic() - 0.4
            if delay > 0:
                threading.Event().wait(delay)
            replacement_margins.append(deadline - time.monotonic())
        return real_protocol(*args, **kwargs)  # type: ignore[arg-type]

    def fresh_deadline(
        coordinator: lsp_process._LifecycleCoordinator,
    ) -> float:
        deadline = real_fresh_deadline(coordinator)
        fresh_deadlines.append(deadline)
        return deadline

    def commit(
        instance: LspProcess,
        candidate: lsp_process._Generation,
        generation_state: ProcessState,
        deadline: float,
    ) -> None:
        commit_deadlines.append(deadline)
        real_commit(instance, candidate, generation_state, deadline)

    monkeypatch.setattr(lsp_process, "LspProtocol", delayed_protocol)
    monkeypatch.setattr(lsp_process, "_fresh_bootstrap_deadline", fresh_deadline)
    monkeypatch.setattr(lsp_process, "_commit_restart_generation_owned", commit)
    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--query-crash-once-marker",
            str(marker),
            "--require-initialized-query",
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
        bootstrap_timeout_seconds=0.8,
    )
    first_nonce = process.generation_nonce
    try:
        assert process.request(
            "initialized/query",
            {"delayed": True},
            deadline=time.monotonic() + 5,
        )["initialized"] is True

        assert process.restart_count == 1
        assert process.generation_nonce != first_nonce
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert len(fresh_deadlines) == 1
        replacement_deadline = fresh_deadlines[0]
        assert 0.25 <= replacement_margins[0] < 0.5
        assert protocol_deadlines[1] == replacement_deadline
        assert bootstraps[1][1] == replacement_deadline
        assert commit_deadlines == [replacement_deadline]
        assert len(protocol_deadlines) == len(bootstraps) == 2
        assert not (process.owner_root / "failure.json").exists()
    finally:
        process.close(time.monotonic() + 5)


def test_generation_bound_notification_refuses_replacement_generation(
    tmp_path: Path,
) -> None:
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
    )
    first_nonce = process.generation_nonce
    try:
        process.restart(time.monotonic() + 5)

        assert process.generation_nonce != first_nonce
        assert process.notify_generation(
            "initialized/noop",
            {},
            generation_nonce=first_nonce,
            deadline=time.monotonic() + 1,
        ) is False
    finally:
        process.close(time.monotonic() + 5)


def test_workspace_ready_promotion_rejects_pending_terminal_failure(
    tmp_path: Path,
) -> None:
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
    )
    coordinator = process._coordinator
    try:
        with coordinator.driver:
            assert lsp_process._enqueue_failure_intent(
                coordinator,
                lsp_process._FailureIntent(
                    process.generation_nonce,
                    "test terminal race",
                    False,
                    time.monotonic(),
                ),
            )

            assert process.promote_workspace_ready(
                generation_nonce=process.generation_nonce,
                deadline=time.monotonic() + 1,
            ) is False
            assert process.state is ProcessState.PROTOCOL_INITIALIZED

            coordinator.failure_queue.get_nowait()
            lsp_process._acknowledge_failure_intent(coordinator)
    finally:
        process.close(time.monotonic() + 5)


def test_generation_guard_wraps_each_autonomous_generation_without_transition_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_HEARTBEAT_SECONDS", 60.0)
    marker = tmp_path / "query-crashed"
    coordinators: list[lsp_process._LifecycleCoordinator] = []
    events: list[tuple[str, str]] = []
    deadlines: dict[str, dict[str, float]] = {}
    lock_snapshots: list[tuple[str, bool, bool, bool]] = []
    active_guard: list[str] = []
    real_register = lsp_process._register_startup_cleanup
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__

    def capture_coordinator(coordinator: lsp_process._LifecycleCoordinator) -> None:
        coordinators.append(coordinator)
        real_register(coordinator)

    def lock_is_available(lock: object) -> bool:
        acquired = lock.acquire(blocking=False)  # type: ignore[attr-defined]
        if acquired:
            lock.release()  # type: ignore[attr-defined]
        return acquired

    def record_locks(stage: str) -> None:
        coordinator = coordinators[0]
        with ThreadPoolExecutor(max_workers=3) as pool:
            driver = pool.submit(lock_is_available, coordinator.driver)
            lifecycle = pool.submit(lock_is_available, coordinator.lock)
            lease = pool.submit(lock_is_available, coordinator.lease_lock)
        lock_snapshots.append(
            (stage, driver.result(), lifecycle.result(), lease.result())
        )

    class Guard:
        def __init__(self, nonce: str, deadline: float) -> None:
            self.nonce = nonce
            self.deadline = deadline

        def __enter__(self) -> Guard:
            record_locks("enter")
            assert active_guard == []
            active_guard.append(self.nonce)
            events.append(("enter", self.nonce))
            deadlines[self.nonce]["enter"] = self.deadline
            return self

        def __exit__(self, *_error: object) -> None:
            record_locks("exit")
            coordinator = coordinators[0]
            with coordinator.condition:
                assert coordinator.active is None or coordinator.active.nonce != self.nonce
                assert coordinator.candidate is not None
                assert coordinator.candidate.nonce == self.nonce
            events.append(("exit", self.nonce))
            deadlines[self.nonce]["exit"] = self.deadline
            assert active_guard == [self.nonce]
            active_guard.clear()

    def guard_factory(generation_nonce: str, deadline: float) -> Guard:
        record_locks("factory")
        events.append(("factory", generation_nonce))
        deadlines[generation_nonce] = {"factory": deadline}
        return Guard(generation_nonce, deadline)

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process.ProcessTree:
        assert len(active_guard) == 1
        events.append(("spawn", active_guard[0]))
        deadlines[active_guard[0]]["spawn"] = deadline
        return real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        assert active_guard == [generation_nonce]
        events.append(("bootstrap", generation_nonce))
        deadlines[generation_nonce]["bootstrap"] = deadline
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    monkeypatch.setattr(lsp_process, "_register_startup_cleanup", capture_coordinator)
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--query-crash-once-marker",
            str(marker),
            "--require-initialized-query",
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
        generation_guard=guard_factory,
    )
    first_nonce = process.generation_nonce
    try:
        assert process.request(
            "initialized/query", {}, deadline=time.monotonic() + 5
        )["initialized"] is True
        second_nonce = process.generation_nonce

        assert second_nonce != first_nonce
        for nonce in (first_nonce, second_nonce):
            _assert_guard_stages(events, deadlines, nonce)
        assert lock_snapshots == [
            (stage, False, True, True)
            for stage in ("factory", "enter", "exit", "factory", "enter", "exit")
        ]
        assert process.restart_count == 1
    finally:
        process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX verified launch descriptor")
def test_generation_guard_launches_from_inherited_descriptor_after_path_replacement(
    tmp_path: Path,
) -> None:
    server = tmp_path / "server.py"
    retired = tmp_path / "retired.py"
    server.write_bytes(FAKE_SERVER.read_bytes())
    descriptors: list[int] = []

    class Guard:
        def __enter__(self) -> object:
            descriptor = os.open(server, os.O_RDONLY)
            descriptors.append(descriptor)
            server.replace(retired)
            server.write_text("raise SystemExit(73)\n", encoding="utf-8")
            descriptor_path = (
                f"/proc/self/fd/{descriptor}"
                if Path("/proc/self/fd").is_dir()
                else f"/dev/fd/{descriptor}"
            )
            return lsp_process.GenerationLaunch(
                (
                    sys.executable,
                    descriptor_path,
                    "--lifecycle",
                    "--bootstrap-handshake",
                ),
                (descriptor,),
            )

        def __exit__(self, *_error: object) -> None:
            os.close(descriptors.pop())

    process = LspProcess.start_configured(
        (sys.executable, str(server), "--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
        generation_guard=lambda _nonce, _deadline: Guard(),
    )
    try:
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        process.close(time.monotonic() + 5)


def test_generation_guard_command_override_is_bounded_before_spawn(
    tmp_path: Path,
) -> None:
    command = tuple(_command("--lifecycle"))
    launch = lsp_process.GenerationLaunch(
        (command[0], "x" * (64 * 1024 + 1)),
    )

    with pytest.raises(ValueError, match="generation launch command"):
        lsp_process._generation_launch(command, launch, cwd=tmp_path)


def test_generation_guard_enter_failure_prevents_spawn_and_cleans_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinators: list[lsp_process._LifecycleCoordinator] = []
    enters = 0
    exits = 0
    real_register = lsp_process._register_startup_cleanup

    def capture_coordinator(coordinator: lsp_process._LifecycleCoordinator) -> None:
        coordinators.append(coordinator)
        real_register(coordinator)

    class FailingGuard:
        def __enter__(self) -> FailingGuard:
            nonlocal enters
            enters += 1
            raise RuntimeError("generation guard precheck failed")

        def __exit__(self, *_error: object) -> None:
            nonlocal exits
            exits += 1

    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("generation guard failure spawned a child")

    monkeypatch.setattr(lsp_process, "_register_startup_cleanup", capture_coordinator)
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(forbidden_spawn),
    )
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="generation guard precheck failed"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + 5,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=lambda *_args: ProcessState.PROCESS_RUNNING,
            generation_guard=lambda _nonce, _deadline: FailingGuard(),
        )

    assert enters == 1
    assert exits == 0
    assert len(coordinators) == 1
    assert coordinators[0].candidate is None
    assert not lsp_process._coordinator_has_ownership(coordinators[0])
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_generation_guard_rejects_non_context_result_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an invalid generation guard spawned a child")

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(forbidden_spawn),
    )
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(TypeError, match="context manager"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + 5,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=lambda *_args: ProcessState.PROCESS_RUNNING,
            generation_guard=lambda _nonce, _deadline: object(),
        )

    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_generation_guard_exit_failure_cleans_replacement_without_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trees: list[lsp_process.ProcessTree] = []
    guard_nonces: list[str] = []
    exited_nonces: list[str] = []
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process.ProcessTree:
        tree = real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    class Guard:
        def __init__(self, nonce: str) -> None:
            self.nonce = nonce

        def __enter__(self) -> Guard:
            return self

        def __exit__(self, error_type: object, *_error: object) -> None:
            exited_nonces.append(self.nonce)
            if len(exited_nonces) == 2 and error_type is None:
                raise RuntimeError("generation guard postcheck failed")

    def guard_factory(generation_nonce: str, _deadline: float) -> Guard:
        guard_nonces.append(generation_nonce)
        return Guard(generation_nonce)

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
        generation_guard=guard_factory,
    )
    first_nonce = process.generation_nonce

    with pytest.raises(RuntimeError, match="generation guard postcheck failed"):
        process.restart(time.monotonic() + 5)

    coordinator = process._coordinator
    assert len(trees) == 2
    assert all(tree.process.poll() is not None for tree in trees)
    assert len(guard_nonces) == len(exited_nonces) == 2
    assert guard_nonces == exited_nonces
    assert process.generation_nonce == first_nonce
    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    assert coordinator.active is None
    assert coordinator.candidate is None
    assert not (process.owner_root / "lease.json").exists()
    assert not lsp_process._coordinator_has_ownership(coordinator)


def test_generation_guard_cannot_suppress_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinators: list[lsp_process._LifecycleCoordinator] = []
    exit_errors: list[type[BaseException] | None] = []
    real_register = lsp_process._register_startup_cleanup

    def capture_coordinator(coordinator: lsp_process._LifecycleCoordinator) -> None:
        coordinators.append(coordinator)
        real_register(coordinator)

    class SuppressingGuard:
        def __enter__(self) -> SuppressingGuard:
            return self

        def __exit__(
            self,
            error_type: type[BaseException] | None,
            *_error: object,
        ) -> bool:
            exit_errors.append(error_type)
            return True

    def fail_bootstrap(*_args: object) -> ProcessState:
        raise RuntimeError("generation bootstrap failed")

    monkeypatch.setattr(lsp_process, "_register_startup_cleanup", capture_coordinator)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="generation bootstrap failed"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + 5,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=fail_bootstrap,
            generation_guard=lambda _nonce, _deadline: SuppressingGuard(),
        )

    assert exit_errors == [RuntimeError]
    assert len(coordinators) == 1
    assert coordinators[0].active is None
    assert coordinators[0].candidate is None
    assert not lsp_process._coordinator_has_ownership(coordinators[0])
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_generation_guard_body_interruption_outranks_exit_error() -> None:
    ordinary_error = RuntimeError("ordinary generation guard exit error")

    class Guard:
        def __enter__(self) -> Guard:
            return self

        def __exit__(self, *_error: object) -> None:
            raise ordinary_error

    configuration = dataclasses.replace(
        lsp_process._unconfigured_generation(),
        generation_guard=lambda _nonce, _deadline: Guard(),
    )

    def interrupt_guard_body() -> None:
        with lsp_process._generation_guard_context(
            configuration,
            "a" * 32,
            time.monotonic() + 1,
        ):
            raise KeyboardInterrupt("generation guard body interrupted")

    with pytest.raises(
        KeyboardInterrupt,
        match="generation guard body interrupted",
    ) as raised:
        interrupt_guard_body()

    assert raised.value.__cause__ is ordinary_error
    traceback_names: list[str] = []
    current = raised.value.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "interrupt_guard_body" in traceback_names


def test_generation_guard_unwraps_body_interruption_without_exception_cycle() -> None:
    cleanup_error = RuntimeError("ordinary generation guard cleanup error")

    class Guard:
        def __enter__(self) -> Guard:
            return self

        def __exit__(self, *_error: object) -> None:
            raise cleanup_error

    configuration = dataclasses.replace(
        lsp_process._unconfigured_generation(),
        generation_guard=lambda _nonce, _deadline: Guard(),
    )

    def make_interruption() -> SystemExit:
        try:
            raise SystemExit(31)
        except SystemExit as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("generation guard body wrapper")
    wrapper.__cause__ = interruption

    with pytest.raises(SystemExit) as raised:
        with lsp_process._generation_guard_context(
            configuration,
            "b" * 32,
            time.monotonic() + 1,
        ):
            raise wrapper

    assert raised.value is interruption
    assert raised.value.__cause__ is cleanup_error
    assert cleanup_error.__cause__ is None
    assert cleanup_error.__context__ is None
    assert wrapper.__cause__ is interruption


def test_startup_unwraps_interruption_without_revisiting_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped process startup interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("process startup interruption wrapper")
    wrapper.__cause__ = interruption

    def interrupt_lifecycle_workers(
        _instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        raise wrapper

    monkeypatch.setattr(
        lsp_process,
        "_start_lifecycle_workers",
        interrupt_lifecycle_workers,
    )
    owner_root = tmp_path / OWNER_NONCE

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped process startup interruption",
    ) as raised:
        _start(tmp_path, "--lifecycle")

    pending = [raised.value]
    seen: set[int] = set()
    reachable: list[BaseException] = []
    while pending:
        current = pending.pop()
        assert id(current) not in seen
        seen.add(id(current))
        reachable.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)

    assert raised.value is interruption
    assert wrapper in reachable
    assert wrapper.__cause__ is None
    assert wrapper.__context__ is None
    assert (owner_root / "failure.json").is_file()
    assert not (owner_root / "lease.json").exists()


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"deadline": math.nan}, ValueError),
        ({"deadline": math.inf}, ValueError),
        ({"deadline": time.monotonic() - 1}, ValueError),
        ({"deadline": True}, TypeError),
        ({"server_request_handlers": []}, TypeError),
        ({"server_request_handlers": {1: lambda _params: None}}, ValueError),
        ({"server_request_handlers": {"": lambda _params: None}}, ValueError),
        ({"server_request_handlers": {"workspace/configuration": False}}, TypeError),
        ({"server_notification_handlers": {"$/progress": False}}, TypeError),
        ({"generation_bootstrap": False}, TypeError),
        ({"generation_guard": False}, TypeError),
    ],
)
def test_configured_start_validates_deadline_handlers_and_bootstrap_before_owner_creation(
    tmp_path: Path,
    overrides: dict[str, object],
    error: type[Exception],
) -> None:
    owner = tmp_path / OWNER_NONCE
    options: dict[str, object] = {
        "deadline": time.monotonic() + 5,
        "server_request_handlers": {},
        "server_notification_handlers": {},
        "generation_bootstrap": lambda *_args: ProcessState.PROCESS_RUNNING,
    }
    options.update(overrides)

    with pytest.raises(error):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            **options,
        )  # type: ignore[arg-type]

    assert not owner.exists()


@pytest.mark.parametrize(
    ("handlers", "owner_nonce"),
    [
        ({}, "b" * 32),
        ({"workspace/configuration": lambda _params: False}, "c" * 32),
    ],
)
def test_bootstrap_configuration_request_rejects_missing_or_false_handler(
    tmp_path: Path,
    handlers: dict[str, object],
    owner_nonce: str,
) -> None:
    owner = tmp_path / owner_nonce

    with pytest.raises(JsonRpcResponseError, match="configuration required"):
        LspProcess.start_configured(
            _command("--lifecycle", "--bootstrap-handshake"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + 5,
            server_request_handlers=handlers,
            server_notification_handlers={},
            generation_bootstrap=_initialize_generation,
        )  # type: ignore[arg-type]

    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_configured_handler_maps_are_immutable_snapshots_that_survive_restart(
    tmp_path: Path,
) -> None:
    configurations: list[object] = []
    progress: list[object] = []
    bootstraps: list[str] = []

    def configuration(params: object) -> bool:
        configurations.append(params)
        return True

    request_handlers = {"workspace/configuration": configuration}
    notification_handlers = {"$/progress": progress.append}

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append(generation_nonce)
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers=request_handlers,
        server_notification_handlers=notification_handlers,
        generation_bootstrap=bootstrap,
    )
    configuration_snapshot = process._coordinator.generation_configuration
    try:
        with pytest.raises(TypeError):
            configuration_snapshot.server_request_handlers["workspace/configuration"] = False  # type: ignore[index]
        with pytest.raises(TypeError):
            configuration_snapshot.server_notification_handlers["$/progress"] = False  # type: ignore[index]

        request_handlers["workspace/configuration"] = lambda _params: False
        notification_handlers.clear()
        process.restart(time.monotonic() + 5)

        assert len(configurations) == 2
        assert len(progress) == 2
        assert len(bootstraps) == 2
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        process.close(time.monotonic() + 5)


def test_configured_process_running_state_is_an_explicit_noop_bootstrap(
    tmp_path: Path,
) -> None:
    calls: list[tuple[int, str, float]] = []

    def noop(
        _protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        calls.append((pid, generation_nonce, deadline))
        return ProcessState.PROCESS_RUNNING

    process = LspProcess.start_configured(
        _command("--lifecycle"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={},
        server_notification_handlers={},
        generation_bootstrap=noop,
    )
    try:
        assert calls == [(process.process.pid, process.generation_nonce, calls[0][2])]
        assert process.state is ProcessState.PROCESS_RUNNING
    finally:
        process.close(time.monotonic() + 5)


@pytest.mark.parametrize(
    ("returned", "error", "owner_nonce"),
    [
        (ProcessState.DEGRADED, ValueError, "d" * 32),
        (ProcessState.FAILED, ValueError, "e" * 32),
        ("workspace_ready", TypeError, "f" * 32),
    ],
)
def test_configured_start_rejects_non_active_bootstrap_state_and_cleans_candidate(
    tmp_path: Path,
    returned: object,
    error: type[Exception],
    owner_nonce: str,
) -> None:
    owner = tmp_path / owner_nonce

    with pytest.raises(error):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + 5,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=lambda *_args: returned,
        )  # type: ignore[arg-type]

    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_configured_start_passes_caller_deadline_to_tree_protocol_and_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deadlines: dict[str, float] = {}
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__
    real_protocol = lsp_process.LspProtocol
    real_create = lsp_process._OwnerDirectory.create

    def record_owner_create(
        owner: lsp_process._OwnerDirectory,
        deadline: float,
    ) -> None:
        deadlines["owner"] = deadline
        real_create(owner, deadline)

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process.ProcessTree:
        deadlines["spawn"] = deadline
        return real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]

    def record_protocol(*args: object, **kwargs: object) -> lsp_protocol.LspProtocol:
        deadlines["protocol"] = kwargs["_startup_deadline"]  # type: ignore[assignment]
        return real_protocol(*args, **kwargs)  # type: ignore[arg-type]

    def bootstrap(*args: object) -> ProcessState:
        deadlines["bootstrap"] = args[-1]  # type: ignore[assignment]
        return ProcessState.PROCESS_RUNNING

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    monkeypatch.setattr(lsp_process._OwnerDirectory, "create", record_owner_create)
    monkeypatch.setattr(lsp_process, "LspProtocol", record_protocol)
    deadline = time.monotonic() + 5
    process = LspProcess.start_configured(
        _command("--lifecycle"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=deadline,
        server_request_handlers={},
        server_notification_handlers={},
        generation_bootstrap=bootstrap,
    )
    try:
        assert deadlines == {
            "owner": deadline,
            "spawn": deadline,
            "protocol": deadline,
            "bootstrap": deadline,
        }
    finally:
        process.close(time.monotonic() + 5)


def test_configured_start_preserves_explicit_autonomous_bootstrap_budget(
    tmp_path: Path,
) -> None:
    process = LspProcess.start_configured(
        _command("--lifecycle"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={},
        server_notification_handlers={},
        generation_bootstrap=lambda *_args: ProcessState.PROCESS_RUNNING,
        bootstrap_timeout_seconds=1.25,
    )
    try:
        configuration = process._coordinator.generation_configuration
        assert configuration.bootstrap_timeout_seconds == 1.25
        started = time.monotonic()
        restart_deadline = lsp_process._fresh_bootstrap_deadline(
            process._coordinator
        )
        assert started + 1.20 <= restart_deadline <= started + 1.30
    finally:
        process.close(time.monotonic() + 5)


def test_bootstrap_timeout_cleans_initial_candidate_and_retains_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def record_child(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def timeout(*_args: object) -> ProcessState:
        raise TimeoutError("generation bootstrap timed out")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", record_child)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(TimeoutError, match="generation bootstrap timed out"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + 5,
            server_request_handlers={},
            server_notification_handlers={},
            generation_bootstrap=timeout,
        )

    assert len(children) == 1
    assert children[0].poll() is not None
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()


def test_restart_bootstrap_failure_is_terminal_and_never_replays_request(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "query-crashed"
    event_log = tmp_path / "events.txt"
    secret = str(tmp_path / "private-restart-secret")
    bootstraps: list[tuple[int, str]] = []

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append((pid, generation_nonce))
        if len(bootstraps) == 2:
            raise RuntimeError(f"second generation bootstrap failed: {secret}")
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--query-crash-once-marker",
            str(marker),
            "--require-initialized-query",
            "--event-log",
            str(event_log),
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    coordinator = process._coordinator

    with pytest.raises(
        ProtocolViolation,
        match="^LSP replacement startup failed$",
    ) as raised:
        process.request("initialized/query", {}, deadline=time.monotonic() + 5)

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "LSP replacement startup cause (RuntimeError)"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value.__cause__)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
        timeout=5,
    )
    assert event_log.read_text(encoding="utf-8").splitlines().count(
        "initialized/query"
    ) == 1
    assert len(bootstraps) == 2
    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    assert coordinator.active is None
    assert coordinator.candidate is None
    assert json.loads((process.owner_root / "failure.json").read_bytes())["code"] == (
        "restart_failed"
    )
    assert not (process.owner_root / "lease.json").exists()


def test_restart_bootstrap_failure_gives_every_waiter_a_fresh_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "concurrent-query-crashed"
    secret = str(tmp_path / "private-concurrent-restart-secret")
    bootstraps = 0

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        nonlocal bootstraps
        bootstraps += 1
        if bootstraps == 2:
            raise RuntimeError(f"replacement bootstrap failed: {secret}")
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--query-crash-once-marker",
            str(marker),
            "--require-initialized-query",
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    protocol_request = process.protocol.request
    request_barrier = threading.Barrier(2)

    def synchronized_request(
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: object = None,
    ) -> object:
        if method == "initialized/query":
            request_barrier.wait(timeout=2)
        return protocol_request(
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,  # type: ignore[arg-type]
        )

    def capture_failure(index: int) -> BaseException:
        try:
            process.request(
                "initialized/query",
                {"waiter": index},
                deadline=time.monotonic() + 5,
            )
        except BaseException as error:
            return error
        raise AssertionError("request unexpectedly survived failed replacement startup")

    monkeypatch.setattr(process.protocol, "request", synchronized_request)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(capture_failure, index) for index in range(2)]
            errors = [future.result(timeout=5) for future in futures]
    finally:
        if lsp_process._coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)

    assert bootstraps == 2
    _assert_distinct_sanitized_failures(errors)
    causes = _assert_single_cause(errors)
    assert secret not in repr(errors)
    assert secret not in repr(causes)


def test_autonomous_bootstrap_uses_configured_budget_and_retains_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 0.15)
    monkeypatch.setattr(lsp_process, "_RECOVERY_RETRY_SECONDS", 0.02)
    bootstraps: list[tuple[float, float, int]] = []
    replacement_bootstrap_started = threading.Event()
    first_cleanup_failed = threading.Event()
    cleanup_retry_started = threading.Event()
    allow_cleanup_retry = threading.Event()

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        _generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        started = time.monotonic()
        bootstraps.append((started, deadline, pid))
        if len(bootstraps) == 1:
            return _initialize_generation(protocol, pid, _generation_nonce, deadline)
        replacement_bootstrap_started.set()
        threading.Event().wait(max(0.0, deadline - time.monotonic()) + 0.01)
        raise TimeoutError("autonomous replacement bootstrap expired")

    configured_deadline = time.monotonic() + 0.8
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=configured_deadline,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    coordinator = process._coordinator
    configured_budget = coordinator.generation_configuration.bootstrap_timeout_seconds
    assert configured_budget is not None
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    assert recovery is not None and heartbeat is not None
    terminate = lsp_process.ProcessTree.terminate
    write_failure_record = lsp_process._write_failure_record
    fresh_cleanup_deadline = lsp_process._fresh_cleanup_deadline
    failure_deadlines: list[float] = []
    cleanup_deadline_margins: list[float] = []
    replacement_terminate_calls = 0

    def record_failure_deadline() -> float:
        deadline = fresh_cleanup_deadline()
        failure_deadlines.append(deadline)
        return deadline

    def consume_failure_deadline(*args: object, **kwargs: object) -> None:
        assert failure_deadlines
        threading.Event().wait(
            max(0.0, failure_deadlines[0] - time.monotonic() - 0.01)
        )
        write_failure_record(*args, **kwargs)  # type: ignore[arg-type]

    def fail_first_replacement_cleanup(current: object, *, deadline: float) -> None:
        nonlocal replacement_terminate_calls
        if _is_replacement_process(current, bootstraps):
            replacement_terminate_calls += 1
            cleanup_deadline_margins.append(deadline - time.monotonic())
            if replacement_terminate_calls == 1:
                first_cleanup_failed.set()
                raise OSError("transient replacement cleanup failure")
            cleanup_retry_started.set()
            _await_release(
                allow_cleanup_retry,
                deadline,
                "replacement cleanup retry stayed blocked",
            )
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "terminate",
        fail_first_replacement_cleanup,
    )
    monkeypatch.setattr(lsp_process, "_fresh_cleanup_deadline", record_failure_deadline)
    monkeypatch.setattr(lsp_process, "_write_failure_record", consume_failure_deadline)
    try:
        process.protocol._become_fatal("trigger autonomous configured restart")
        assert replacement_bootstrap_started.wait(2)
        assert first_cleanup_failed.wait(2)

        replacement_started, replacement_deadline, _pid = bootstraps[1]
        assert 0 < replacement_deadline - replacement_started <= configured_budget
        assert cleanup_deadline_margins[0] >= 0.05
        assert cleanup_retry_started.wait(2)
        assert coordinator.phase in {
            lsp_process._LifecyclePhase.CLEANUP_PENDING,
            lsp_process._LifecyclePhase.STOPPING_FAILURE,
        }
        assert coordinator.recovery_thread is recovery
        assert recovery.is_alive()
        assert heartbeat.is_alive()
        assert lsp_process._coordinator_has_ownership(coordinator)

        allow_cleanup_retry.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(1)
        heartbeat.join(1)
        assert not recovery.is_alive()
        assert not heartbeat.is_alive()
        assert coordinator.recovery_thread is None
        assert coordinator.heartbeat_thread is None
        assert coordinator.cleanup_result.ownership_pending is False
        assert not (process.owner_root / "lease.json").exists()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        allow_cleanup_retry.set()
        _close_owned_process(process, coordinator)


def test_caller_restart_failure_keeps_deadline_and_retains_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 5.0)
    monkeypatch.setattr(lsp_process, "_RECOVERY_RETRY_SECONDS", 0.02)
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    assert recovery is not None
    write_failure_record = lsp_process._write_failure_record
    terminate = lsp_process.ProcessTree.terminate
    evidence_started = threading.Event()
    cleanup_started = threading.Event()
    autonomous_cleanup_started = threading.Event()
    allow_autonomous_cleanup = threading.Event()
    caller_finished = threading.Event()
    cleanup_deadlines: list[float] = []
    cleanup_threads: list[threading.Thread] = []
    restart_errors: list[BaseException] = []
    restart_elapsed: list[float] = []
    caller_deadline = time.monotonic() + 0.05

    def fail_restart_prepare(
        _instance: LspProcess,
        _deadline: float,
    ) -> tuple[lsp_process._Generation, ProcessState]:
        raise OSError("caller restart candidate failed")

    def delay_failure_evidence(*args: object, **kwargs: object) -> None:
        evidence_started.set()
        threading.Event().wait(
            max(0.0, caller_deadline - time.monotonic() + 0.01)
        )
        write_failure_record(*args, **kwargs)  # type: ignore[arg-type]

    def block_fresh_cleanup(current: object, *, deadline: float) -> None:
        cleanup_deadlines.append(deadline)
        cleanup_threads.append(threading.current_thread())
        cleanup_started.set()
        if deadline <= caller_deadline:
            raise TimeoutError("caller restart cleanup deadline expired")
        autonomous_cleanup_started.set()
        if not allow_autonomous_cleanup.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("autonomous restart cleanup stayed blocked")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    def restart() -> None:
        started = time.monotonic()
        try:
            process.restart(caller_deadline)
        except BaseException as error:
            restart_errors.append(error)
        finally:
            restart_elapsed.append(time.monotonic() - started)
            caller_finished.set()

    monkeypatch.setattr(
        lsp_process,
        "_prepare_restart_generation_owned",
        fail_restart_prepare,
    )
    monkeypatch.setattr(lsp_process, "_write_failure_record", delay_failure_evidence)
    monkeypatch.setattr(lsp_process.ProcessTree, "terminate", block_fresh_cleanup)
    caller = threading.Thread(target=restart)
    caller.start()
    try:
        assert evidence_started.wait(1)
        assert cleanup_started.wait(1)
        assert caller_finished.wait(0.5)
        assert not caller.is_alive()
        assert len(restart_errors) == 1
        assert type(restart_errors[0]) is OSError
        assert str(restart_errors[0]) == "caller restart candidate failed"
        assert restart_elapsed[0] < 0.5
        assert cleanup_threads[0] is caller
        assert cleanup_deadlines[0] == caller_deadline

        assert autonomous_cleanup_started.wait(2)
        assert recovery in cleanup_threads[1:]
        assert recovery.is_alive()
        assert lsp_process._coordinator_has_ownership(coordinator)

        allow_autonomous_cleanup.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(1)
        assert not recovery.is_alive()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        allow_autonomous_cleanup.set()
        caller.join(5)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_concurrent_autonomous_fatals_bootstrap_only_one_replacement(
    tmp_path: Path,
) -> None:
    bootstraps: list[tuple[int, str]] = []

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append((pid, generation_nonce))
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    protocol = process.protocol
    barrier = threading.Barrier(8)

    def become_fatal(index: int) -> None:
        barrier.wait()
        protocol._become_fatal(f"concurrent fatal {index}")

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(become_fatal, range(8)))

        assert _coordinator_wait(process, lambda: process.restart_count == 1, timeout=5)
        assert len(bootstraps) == 2
        assert bootstraps[0][0] != bootstraps[1][0]
        assert bootstraps[0][1] != bootstraps[1][1]
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        process.close(time.monotonic() + 5)


def test_explicit_restart_and_autonomous_wake_bootstrap_one_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bootstraps: list[tuple[int, str]] = []

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append((pid, generation_nonce))
        _initialize_generation(protocol, pid, generation_nonce, deadline)
        return ProcessState.WORKSPACE_READY

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    coordinator = process._coordinator
    first_protocol = process.protocol
    real_restart_generation = lsp_process._restart_generation
    real_process_recovery_request = lsp_process._process_recovery_request
    explicit_paused = threading.Event()
    release_explicit = threading.Event()
    autonomous_entered = threading.Event()
    autonomous_replacement_entered = threading.Event()
    autonomous_replacement_finished = threading.Event()
    explicit_threads: list[threading.Thread] = []
    explicit_errors: list[BaseException] = []

    def pause_explicit_restart(instance: LspProcess, deadline: float) -> None:
        current = threading.current_thread()
        if current is explicit_threads[0]:
            explicit_paused.set()
            assert release_explicit.wait(max(0.0, deadline - time.monotonic()))
        elif current is coordinator.recovery_thread:
            autonomous_replacement_entered.set()
        try:
            real_restart_generation(instance, deadline)
        finally:
            if current is coordinator.recovery_thread:
                autonomous_replacement_finished.set()

    def observe_autonomous_recovery(
        instance: LspProcess,
        deadline: float,
    ) -> tuple[bool, str | None]:
        autonomous_entered.set()
        return real_process_recovery_request(instance, deadline)

    def explicit_restart() -> None:
        try:
            process.restart(time.monotonic() + 10)
        except BaseException as error:
            explicit_errors.append(error)

    monkeypatch.setattr(lsp_process, "_restart_generation", pause_explicit_restart)
    monkeypatch.setattr(
        lsp_process, "_process_recovery_request", observe_autonomous_recovery
    )
    restart_thread = threading.Thread(target=explicit_restart)
    explicit_threads.append(restart_thread)
    restart_thread.start()
    try:
        assert explicit_paused.wait(3)
        first_protocol._become_fatal("fatal while explicit restart is pending")
        coordinator.recovery_wake.set()
        assert autonomous_entered.wait(3)
        if autonomous_replacement_entered.wait(0.25):
            assert autonomous_replacement_finished.wait(5)

        release_explicit.set()
        restart_thread.join(10)

        assert not restart_thread.is_alive()
        assert explicit_errors == []
        assert bootstraps == [
            bootstraps[0],
            (process.process.pid, process.generation_nonce),
        ]
        assert len(bootstraps) == 2
        assert process.restart_count == 1
        assert process.state is ProcessState.WORKSPACE_READY
        assert coordinator.phase is lsp_process._LifecyclePhase.RUNNING
        assert coordinator.active is not None
        assert coordinator.candidate is None
        assert coordinator.terminal_outcome is None
    finally:
        release_explicit.set()
        restart_thread.join(10)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_bootstrap_raw_protocol_and_handler_complete_while_driver_is_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinators: list[lsp_process._LifecycleCoordinator] = []
    configurations: list[object] = []
    lock_observations: list[tuple[bool, bool, bool]] = []
    real_prepare = lsp_process._prepare_generation

    def observe_prepare(
        coordinator: lsp_process._LifecycleCoordinator,
        *args: object,
        **kwargs: object,
    ) -> lsp_process._Generation:
        coordinators.append(coordinator)
        return real_prepare(coordinator, *args, **kwargs)  # type: ignore[arg-type]

    def lock_is_available(lock: object) -> bool:
        acquired = lock.acquire(blocking=False)  # type: ignore[attr-defined]
        if acquired:
            lock.release()  # type: ignore[attr-defined]
        return acquired

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        coordinator = coordinators[-1]
        with ThreadPoolExecutor(max_workers=3) as pool:
            driver_available = pool.submit(
                lock_is_available, coordinator.driver
            ).result(timeout=1)
            lifecycle_available = pool.submit(
                lock_is_available, coordinator.lock
            ).result(timeout=1)
            lease_available = pool.submit(
                lock_is_available, coordinator.lease_lock
            ).result(timeout=1)
        lock_observations.append(
            (driver_available, lifecycle_available, lease_available)
        )
        assert driver_available is False
        assert lifecycle_available is True
        assert lease_available is True
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    monkeypatch.setattr(lsp_process, "_prepare_generation", observe_prepare)
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={
            "workspace/configuration": lambda params: configurations.append(params)
            or True
        },
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    try:
        process.restart(time.monotonic() + 5)
        assert len(coordinators) == 2
        assert lock_observations == [(False, True, True), (False, True, True)]
        assert configurations == [
            {"items": [{"section": "python"}]},
            {"items": [{"section": "python"}]},
        ]
        assert process.restart_count == 1
    finally:
        process.close(time.monotonic() + 5)


@pytest.mark.parametrize(
    ("operation", "owner_nonce"),
    [
        ("close", "b" * 32),
        ("restart", "c" * 32),
        ("terminal", "d" * 32),
    ],
)
def test_protocol_callback_lifecycle_operations_fail_fast_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    owner_nonce: str,
) -> None:
    instances: list[LspProcess] = []
    real_start_heartbeat = lsp_process._start_heartbeat_worker
    handler_errors: list[BaseException] = []
    handler_elapsed: list[float] = []
    handler_threads: list[threading.Thread] = []
    lifecycle_snapshots: list[tuple[object, ...]] = []

    def capture_instance(instance: LspProcess, deadline: float) -> None:
        instances.append(instance)
        real_start_heartbeat(instance, deadline)

    def snapshot(instance: LspProcess) -> tuple[object, ...]:
        coordinator = instance._coordinator
        with coordinator.condition:
            candidate = coordinator.candidate
            return (
                instance.state,
                instance.restart_count,
                coordinator.phase,
                coordinator.terminal_outcome,
                coordinator.mandatory_failure_intent,
                coordinator.recovery_attempted,
                coordinator.active,
                candidate.nonce if candidate is not None else None,
            )

    def progress(_params: object) -> None:
        instance = instances[0]
        handler_threads.append(threading.current_thread())
        lifecycle_snapshots.append(snapshot(instance))
        started = time.monotonic()
        try:
            deadline = time.monotonic() + 1
            if operation == "close":
                instance.close(deadline)
            elif operation == "restart":
                instance.restart(deadline)
            else:
                instance._terminal_failure("handler_failure", deadline)
        except BaseException as error:
            handler_errors.append(error)
        finally:
            handler_elapsed.append(time.monotonic() - started)
            lifecycle_snapshots.append(snapshot(instance))

    monkeypatch.setattr(lsp_process, "_start_heartbeat_worker", capture_instance)
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / owner_nonce,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": progress},
        generation_bootstrap=_initialize_generation,
    )
    try:
        assert len(handler_errors) == 1
        assert type(handler_errors[0]) is RuntimeError
        assert str(handler_errors[0]) == (
            "LSP lifecycle operations are not reentrant from protocol callbacks"
        )
        assert handler_elapsed[0] < 0.2
        assert handler_threads == [process.protocol.reader_thread]
        assert lifecycle_snapshots[0] == lifecycle_snapshots[1]
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert process.restart_count == 0
        assert process._coordinator.phase is lsp_process._LifecyclePhase.RUNNING
        assert process._coordinator.terminal_outcome is None
        assert process._coordinator.mandatory_failure_intent is None
    finally:
        process.close(time.monotonic() + 5)


def test_uncaught_progress_handler_lifecycle_error_is_nonfatal_and_bootstrap_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instances: list[LspProcess] = []
    real_start_heartbeat = lsp_process._start_heartbeat_worker
    handler_entered = threading.Event()
    handler_returned_normally = threading.Event()

    def capture_instance(instance: LspProcess, deadline: float) -> None:
        instances.append(instance)
        real_start_heartbeat(instance, deadline)

    def progress(_params: object) -> None:
        handler_entered.set()
        instances[0].close(time.monotonic() + 5)
        handler_returned_normally.set()

    monkeypatch.setattr(lsp_process, "_start_heartbeat_worker", capture_instance)
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / ("e" * 32),
        deadline=time.monotonic() + 2,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": progress},
        generation_bootstrap=_initialize_generation,
    )
    try:
        assert handler_entered.is_set()
        assert not handler_returned_normally.is_set()
        assert process.protocol.fatal is False
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert process._coordinator.phase is lsp_process._LifecyclePhase.RUNNING
        assert process._coordinator.terminal_outcome is None
    finally:
        process.close(time.monotonic() + 5)


@pytest.mark.parametrize(
    ("callback_kind", "operation"),
    [("request", "close"), ("notification", "restart")],
)
def test_startup_callback_rejects_own_lifecycle_before_protocol_constructor_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    callback_kind: str,
    operation: str,
) -> None:
    marker = tmp_path / "enable-startup-callback"
    callback_finished = threading.Event()
    callback_errors: list[BaseException] = []
    callback_elapsed: list[float] = []
    lifecycle_snapshots: list[tuple[object, ...]] = []
    process: LspProcess | None = None

    def snapshot(current: LspProcess) -> tuple[object, ...]:
        coordinator = current._coordinator
        with coordinator.condition:
            candidate = coordinator.candidate
            return (
                current.state,
                current.restart_count,
                coordinator.phase,
                coordinator.terminal_outcome,
                coordinator.mandatory_failure_intent,
                coordinator.recovery_attempted,
                candidate.nonce if candidate is not None else None,
            )

    def invoke_lifecycle() -> None:
        assert process is not None
        lifecycle_snapshots.append(snapshot(process))
        started = time.monotonic()
        try:
            getattr(process, operation)(time.monotonic() + 0.2)
        except BaseException as error:
            callback_errors.append(error)
        finally:
            callback_elapsed.append(time.monotonic() - started)
            lifecycle_snapshots.append(snapshot(process))
            callback_finished.set()

    def request_handler(params: object) -> object:
        if params == {"startup_callback": True}:
            invoke_lifecycle()
        return True

    def notification_handler(params: object) -> None:
        if params == {"startup_callback": True}:
            invoke_lifecycle()

    process = LspProcess.start_configured(
        _command(
            "--lifecycle",
            "--bootstrap-handshake",
            "--startup-callback",
            callback_kind,
            "--startup-callback-marker",
            str(marker),
        ),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": request_handler},
        server_notification_handlers={"$/progress": notification_handler},
        generation_bootstrap=_initialize_generation,
    )
    marker.write_bytes(b"enabled")
    _hold_protocol_constructor_until_callback(monkeypatch, callback_finished)
    try:
        process.restart(time.monotonic() + 5)

        assert callback_finished.is_set()
        assert len(callback_errors) == 1
        assert type(callback_errors[0]) is RuntimeError
        assert str(callback_errors[0]) == (
            "LSP lifecycle operations are not reentrant from protocol callbacks"
        )
        assert callback_elapsed[0] < 0.05
        assert lifecycle_snapshots[0] == lifecycle_snapshots[1]
        assert process.restart_count == 1
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert process.protocol.fatal is False
        assert process._coordinator.terminal_outcome is None
    finally:
        if lsp_process._coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)


def test_startup_callback_can_close_unrelated_process_before_constructor_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    other = LspProcess.start(
        _command("--lifecycle"),
        cwd=tmp_path,
        owner_root=tmp_path / ("f" * 32),
    )
    callback_finished = threading.Event()
    callback_errors: list[BaseException] = []
    callback_elapsed: list[float] = []
    source: LspProcess | None = None

    def request_handler(params: object) -> object:
        if params == {"startup_callback": True}:
            started = time.monotonic()
            try:
                other.close(time.monotonic() + 5)
            except BaseException as error:
                callback_errors.append(error)
            finally:
                callback_elapsed.append(time.monotonic() - started)
                callback_finished.set()
        return True

    _hold_protocol_constructor_until_callback(monkeypatch, callback_finished)
    try:
        source = LspProcess.start_configured(
            _command(
                "--lifecycle",
                "--bootstrap-handshake",
                "--startup-callback",
                "request",
            ),
            cwd=tmp_path,
            owner_root=tmp_path / ("e" * 32),
            deadline=time.monotonic() + 5,
            server_request_handlers={"workspace/configuration": request_handler},
            server_notification_handlers={"$/progress": lambda _params: None},
            generation_bootstrap=_initialize_generation,
        )

        assert callback_finished.is_set()
        assert callback_errors == []
        assert len(callback_elapsed) == 1
        assert source.state is ProcessState.PROTOCOL_INITIALIZED
        assert source.protocol.fatal is False
        assert not lsp_process._coordinator_has_ownership(other._coordinator)
    finally:
        if source is not None and lsp_process._coordinator_has_ownership(
            source._coordinator
        ):
            source.close(time.monotonic() + 5)
        if lsp_process._coordinator_has_ownership(other._coordinator):
            other.close(time.monotonic() + 5)


def test_protocol_callback_markers_restore_nested_scopes_and_exceptions() -> None:
    outer_coordinator = lsp_process._LifecycleCoordinator(None)
    inner_coordinator = lsp_process._LifecycleCoordinator(None)
    outer_process = object.__new__(LspProcess)
    inner_process = object.__new__(LspProcess)
    outer_process._coordinator = outer_coordinator
    inner_process._coordinator = inner_coordinator
    handler_error = ValueError("user handler failed")

    def inner_handler(_params: object) -> None:
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process._reject_protocol_callback_lifecycle(outer_process)
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process._reject_protocol_callback_lifecycle(inner_process)
        raise handler_error

    wrapped_inner = lsp_process._wrap_protocol_callback(
        inner_coordinator, inner_handler
    )

    def outer_handler(params: object) -> object:
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process._reject_protocol_callback_lifecycle(outer_process)
        with pytest.raises(ValueError) as raised:
            wrapped_inner(params)
        assert raised.value is handler_error
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process._reject_protocol_callback_lifecycle(outer_process)
        lsp_process._reject_protocol_callback_lifecycle(inner_process)
        raise handler_error

    wrapped_outer = lsp_process._wrap_protocol_callback(
        outer_coordinator, outer_handler
    )
    with pytest.raises(ValueError) as raised:
        wrapped_outer(object())
    assert raised.value is handler_error
    lsp_process._reject_protocol_callback_lifecycle(outer_process)
    lsp_process._reject_protocol_callback_lifecycle(inner_process)


def test_close_serializes_behind_restart_bootstrap_and_commits_success(
    tmp_path: Path,
) -> None:
    bootstraps: list[str] = []
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    close_finished = threading.Event()
    restart_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append(generation_nonce)
        if len(bootstraps) == 2:
            bootstrap_entered.set()
            assert release_bootstrap.wait(max(0.0, deadline - time.monotonic()))
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    coordinator = process._coordinator

    def restart() -> None:
        try:
            process.restart(time.monotonic() + 8)
        except BaseException as error:
            restart_errors.append(error)

    def close() -> None:
        try:
            process.close(time.monotonic() + 8)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_finished.set()

    restart_thread = threading.Thread(target=restart)
    close_thread = threading.Thread(target=close)
    restart_thread.start()
    assert bootstrap_entered.wait(3)
    close_thread.start()
    close_returned_before_bootstrap = close_finished.wait(0.2)
    release_bootstrap.set()
    restart_thread.join(8)
    close_thread.join(8)
    try:
        assert close_returned_before_bootstrap is False
        assert not restart_thread.is_alive()
        assert not close_thread.is_alive()
        assert restart_errors == []
        assert close_errors == []
        assert len(bootstraps) == 2
        assert coordinator.success_committed is True
        assert coordinator.terminal_outcome == "success"
        assert coordinator.terminal_code is None
        assert coordinator.failure_evidence_identity is None
        assert coordinator.mandatory_failure_intent is None
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_SUCCESS
        assert coordinator.cleanup_result.evidence == "not_applicable"
        assert coordinator.cleanup_result.ownership_pending is False
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert not process.owner_root.exists()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        release_bootstrap.set()
        restart_thread.join(8)
        close_thread.join(8)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_delayed_failure_selection_cannot_mutate_committed_close_success(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    state_before_close = process.state
    process.close(time.monotonic() + 5)
    candidate_process = subprocess.Popen(
        _command("--lifecycle", "--sleep-seconds", "30"),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    candidate = lsp_process._Generation(
        "b" * 32,
        _FakeTree(candidate_process),  # type: ignore[arg-type]
        candidate_process,
    )
    with coordinator.condition:
        coordinator.candidate = candidate
    callback_errors: list[BaseException] = []

    def delayed_failure_callback() -> None:
        try:
            lsp_process._mark_terminal_failure(
                process,
                coordinator,
                "process_exited",
                time.monotonic() + 2,
            )
            lsp_process._fail_restart_generation(process, time.monotonic() + 2)
        except BaseException as error:
            callback_errors.append(error)

    callback = threading.Thread(target=delayed_failure_callback)
    callback.start()
    callback.join(2)

    assert not callback.is_alive()
    assert callback_errors == []
    assert coordinator.success_committed is True
    assert coordinator.terminal_outcome == "success"
    assert coordinator.terminal_code is None
    assert coordinator.failure_evidence_identity is None
    assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_SUCCESS
    assert coordinator.cleanup_result.evidence == "not_applicable"
    assert coordinator.cleanup_result.ownership_pending is False
    assert coordinator.candidate is None
    assert candidate.released
    assert candidate_process.poll() is not None
    assert process.state is state_before_close
    assert not process.owner_root.exists()
    assert not lsp_process._coordinator_has_ownership(coordinator)


def test_restart_lease_names_candidate_before_bootstrap_commit_without_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bootstraps: list[str] = []
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    heartbeat_wrote = threading.Event()

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append(generation_nonce)
        if len(bootstraps) == 2:
            bootstrap_entered.set()
            assert release_bootstrap.wait(3)
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + 5,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    first_nonce = process.generation_nonce
    real_write_lease = lsp_process._write_generation_lease
    heartbeat_generations: list[str] = []
    restart_errors: list[BaseException] = []

    def observe_write_lease(
        owner: lsp_process._OwnerDirectory,
        generation: lsp_process._Generation,
        owner_nonce: str,
        deadline: float,
        retry_stop: threading.Event,
    ) -> None:
        if threading.current_thread() is process._coordinator.heartbeat_thread:
            heartbeat_generations.append(generation.nonce)
            heartbeat_wrote.set()
        real_write_lease(
            owner,
            generation,
            owner_nonce,
            deadline,
            retry_stop,
        )

    def restart() -> None:
        try:
            process.restart(time.monotonic() + 5)
        except BaseException as error:
            restart_errors.append(error)

    monkeypatch.setattr(lsp_process, "_write_generation_lease", observe_write_lease)
    thread = threading.Thread(target=restart)
    thread.start()
    try:
        assert bootstrap_entered.wait(3)
        candidate_nonce = bootstraps[1]
        process._coordinator.heartbeat_wake.set()
        assert heartbeat_wrote.wait(1)
        lease_record = json.loads(
            (process.owner_root / "lease.json").read_bytes()
        )
        with process._coordinator.condition:
            candidate = process._coordinator.candidate
            transition_snapshot = (
                process._coordinator.phase,
                process._coordinator.active,
                candidate.nonce if candidate is not None else None,
                (
                    process._coordinator.lease_generation.nonce
                    if process._coordinator.lease_generation is not None
                    else None
                ),
            )

        assert heartbeat_generations == [candidate_nonce]
        assert lease_record["generation_nonce"] == candidate_nonce
        assert transition_snapshot == (
            lsp_process._LifecyclePhase.RESTARTING,
            None,
            candidate_nonce,
            candidate_nonce,
        )
        assert process.generation_nonce == first_nonce
        assert process.state is ProcessState.DEGRADED
        assert candidate_nonce != first_nonce

        release_bootstrap.set()
        thread.join(5)
        assert not thread.is_alive()
        assert restart_errors == []
        assert process.generation_nonce == candidate_nonce
    finally:
        release_bootstrap.set()
        thread.join(5)
        process.close(time.monotonic() + 5)


def test_legacy_start_signature_budget_and_state_are_unchanged(tmp_path: Path) -> None:
    signature = inspect.signature(LspProcess.start)
    assert tuple(signature.parameters) == ("command", "cwd", "owner_root")
    assert signature.parameters["cwd"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["owner_root"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "deadline" not in signature.parameters
    assert tuple(inspect.signature(LspProcess.start_configured).parameters) == (
        "command",
        "cwd",
        "owner_root",
        "deadline",
        "server_request_handlers",
        "server_notification_handlers",
        "generation_bootstrap",
        "bootstrap_timeout_seconds",
        "generation_guard",
    )

    started = time.monotonic()
    process = _start(tmp_path, "--lifecycle")
    try:
        assert process.state is ProcessState.PROCESS_RUNNING
        assert time.monotonic() - started < lsp_process._STARTUP_WAIT_SECONDS
        assert process._coordinator.generation_configuration.generation_bootstrap is None
        assert process._coordinator.generation_configuration.generation_guard is None
    finally:
        process.close(time.monotonic() + 5)


def test_lifecycle_coordinator_uses_one_authority_lock_and_condition() -> None:
    coordinator = lsp_process._LifecycleCoordinator(None)

    assert coordinator.condition._lock is coordinator.lock
    assert coordinator.driver is not coordinator.lock
    assert not hasattr(coordinator, "driver_owner")
    assert not hasattr(coordinator, "driver_depth")


def test_driver_release_survives_transition_contention_and_cleanup_reacquires() -> None:
    coordinator = lsp_process._LifecycleCoordinator(None)
    owner_acquired = threading.Event()
    held = threading.Event()
    release_owner = threading.Event()
    release_transition = threading.Event()
    owner_done = threading.Event()
    owner_errors: list[BaseException] = []

    def own_driver() -> None:
        try:
            lsp_process._acquire_driver(coordinator, time.monotonic() + 1)
            owner_acquired.set()
            assert release_owner.wait(2)
        except BaseException as error:
            owner_errors.append(error)
        finally:
            try:
                lsp_process._release_driver(coordinator)
            except BaseException as error:
                owner_errors.append(error)
            owner_done.set()

    def hold_lifecycle_lock() -> None:
        with coordinator.condition:
            held.set()
            assert release_transition.wait(2)

    owner = threading.Thread(target=own_driver)
    owner.start()
    assert owner_acquired.wait(1)
    holder = threading.Thread(target=hold_lifecycle_lock)
    holder.start()
    assert held.wait(1)
    expired = time.monotonic() + 0.05
    try:
        assert threading.Event().wait(max(0.0, expired - time.monotonic())) is False
        release_owner.set()
        assert owner_done.wait(0.2)
    finally:
        release_transition.set()
        holder.join(1)
        owner.join(1)

    assert not holder.is_alive()
    assert not owner.is_alive()
    retry_errors = lsp_process._drive_cleanup(
        None,
        time.monotonic() + 0.2,
        terminal=True,
        coordinator_override=coordinator,
    )
    assert owner_errors == []
    assert retry_errors == []


def test_live_lease_is_bounded_redacted_and_removed_after_graceful_close(
    tmp_path: Path,
) -> None:
    event_log = tmp_path / "events.txt"
    process = _start(tmp_path, "--lifecycle", "--event-log", str(event_log))
    lease_path = process.owner_root / "lease.json"
    lease = json.loads(lease_path.read_bytes())

    assert set(lease) == {
        "expires_at",
        "generation_nonce",
        "heartbeat_at",
        "manager_pid",
        "owner_nonce",
        "schema_version",
        "server_pid",
        "state",
    }
    assert lease["manager_pid"] == os.getpid()
    assert lease["server_pid"] == process.process.pid
    assert lease["owner_nonce"] == process.owner_nonce
    assert lease["generation_nonce"] == process.generation_nonce
    assert lease["schema_version"] == 1
    assert lease["state"] == "live"
    assert str(tmp_path) not in lease_path.read_text(encoding="utf-8")

    process.close(time.monotonic() + 5)

    assert event_log.read_text(encoding="utf-8").splitlines() == ["shutdown", "exit"]
    assert not process.owner_root.exists()


def test_shutdown_alone_completes_tree_threads_lease_and_scratch_cleanup(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "shutdown-descendant.pid"
    process = _start(
        tmp_path,
        "--lifecycle",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(pid_file),
    )
    descendant = _await_descendant_pid(pid_file)
    owner_threads = (
        process.protocol.reader_thread,
        process.protocol.writer_thread,
        process._stderr_thread,
        process._exit_thread,
        process._heartbeat_thread,
    )

    process.shutdown(time.monotonic() + 5)
    process.shutdown(time.monotonic() + 5)

    assert process.process.poll() is not None
    _await_pid_exit(descendant)
    assert not _pid_alive(descendant)
    assert not process.owner_root.exists()
    assert all(thread is None or not thread.is_alive() for thread in owner_threads)


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle release")
def test_windows_normal_close_joins_all_users_before_releasing_retained_process_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    generation = process._coordinator.active
    assert generation is not None and generation.protocol is not None
    retained = process.process
    handle = int(retained._handle)
    owner_threads = (
        generation.protocol.reader_thread,
        generation.protocol.writer_thread,
        generation.stderr_thread,
        generation.exit_thread,
    )
    release_observations: list[tuple[bool, ...]] = []

    def close_process_handle(child: subprocess.Popen[bytes]) -> None:
        assert child is retained
        release_observations.append(tuple(thread.is_alive() for thread in owner_threads))
        child._handle.Close()

    monkeypatch.setattr(
        lsp_process._lsp_process_tree,
        "_close_windows_process_handle",
        close_process_handle,
        raising=False,
    )
    assert _windows_handle_status(handle) == (True, 0)

    process.close(time.monotonic() + 5)

    assert release_observations == [(False, False, False, False)]
    assert _windows_handle_status(handle) == (False, 6)
    assert retained.returncode is not None
    assert retained.poll() == retained.returncode
    assert retained.wait(timeout=0) == retained.returncode


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle retry")
def test_windows_process_handle_close_failure_keeps_cleanup_pending_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    assert generation.tree is not None
    retained = process.process
    handle = int(retained._handle)
    real_close = retained._handle.Close
    real_unregister = lsp_process.atexit.unregister
    close_allowed = False
    unregister_calls: list[object] = []

    def close() -> None:
        if not close_allowed:
            raise OSError("process handle close failed")
        real_close()

    def unregister(callback: object) -> None:
        unregister_calls.append(callback)
        real_unregister(callback)

    monkeypatch.setattr(retained._handle, "Close", close)
    monkeypatch.setattr(lsp_process.atexit, "unregister", unregister)
    try:
        with pytest.raises(OSError, match="process handle close failed"):
            process.close(time.monotonic() + 5)

        assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert coordinator.active is generation
        assert generation.tree is not None
        assert generation.process is retained
        assert coordinator.cleanup_result.tree_release == "failed"
        assert (process.owner_root / "lease.json").is_file()
        assert _windows_handle_status(handle) == (True, 0)
        assert unregister_calls == []

        close_allowed = True
        process.close(time.monotonic() + 5)

        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_SUCCESS
        assert coordinator.cleanup_result.tree_release == "success"
        assert _windows_handle_status(handle) == (False, 6)
        assert retained.poll() == retained.returncode
        assert len(unregister_calls) == 1
        assert not process.owner_root.exists()
    finally:
        close_allowed = True
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_request_after_shutdown_cannot_restart_terminal_process(tmp_path: Path) -> None:
    process = _start(tmp_path, "--lifecycle")
    pid = process.process.pid
    generation = process.generation_nonce

    process.shutdown(time.monotonic() + 5)

    with pytest.raises(RuntimeError, match="LSP process is closed"):
        process.request("echo", {}, deadline=time.monotonic() + 5)
    assert process.process.pid == pid
    assert process.generation_nonce == generation
    assert process.restart_count == 0


_OS_INJECTED_ENVIRONMENT = frozenset({"__CF_USER_TEXT_ENCODING"})


def _idle_boundary_probe(process) -> tuple[bool, bool, bool]:
    """Exactly 300 seconds, measured against one pinned idle baseline.

    Reading the baseline is not enough: any use of the process stamps
    `last_used_monotonic`, and a startup or restart worker can still be running
    when the probe starts. Pinning the value first makes a stamp visible as a
    moved baseline instead of a silently wrong comparison.
    """
    baseline = time.monotonic()
    process.last_used_monotonic = baseline
    below = process.idle_expired(baseline + 299.999)
    at_boundary = process.idle_expired(baseline + 300.0)
    return below, at_boundary, process.last_used_monotonic == baseline


def _idle_boundary_holds(process) -> bool:
    below, at_boundary, stable = _idle_boundary_probe(process)
    return stable and below is False and at_boundary is True


def test_idle_expiry_is_exactly_300_seconds_and_rejects_non_finite_input(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    try:
        # A moved baseline means a concurrent stamp, not a wrong boundary, so
        # the last probe is reported rather than a bare `assert False`.
        attempts = [_idle_boundary_holds(process) for _attempt in range(20)]
        assert any(attempts), _idle_boundary_probe(process)
        for invalid in (math.nan, math.inf, True, "later"):
            with pytest.raises((TypeError, ValueError)):
                process.idle_expired(invalid)  # type: ignore[arg-type]
    finally:
        process.close(time.monotonic() + 5)


def test_close_forces_stubborn_process_tree_within_caller_deadline(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "descendant.pid"
    process = _start(
        tmp_path,
        "--lifecycle",
        "--ignore-shutdown",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(pid_file),
        "--sleep-seconds",
        "30",
    )
    descendant = _await_descendant_pid(pid_file)
    deadline = time.monotonic() + 2.5

    process.close(deadline)

    assert time.monotonic() <= deadline + 0.2
    assert process.process.poll() is not None
    assert not _pid_alive(descendant)


def test_shutdown_poll_failure_still_forces_complete_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    real_poll = process.process.poll
    poll_calls = 0

    def fail_first_poll() -> int | None:
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            raise OSError("graceful poll failed")
        return real_poll()

    monkeypatch.setattr(process.process, "poll", fail_first_poll)

    process.shutdown(time.monotonic() + 5)

    assert poll_calls >= 2
    assert real_poll() is not None
    assert not process.owner_root.exists()


def test_shutdown_graceful_interruption_outranks_ordinary_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    protocol = process.protocol
    request = lsp_protocol.LspProtocol.request
    drive_cleanup = lsp_process._drive_cleanup
    ordinary_error = RuntimeError("ordinary forced cleanup error")

    def interrupt_shutdown_request(
        current: lsp_protocol.LspProtocol,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: object = None,
    ) -> object:
        if current is protocol and method == "shutdown":
            raise KeyboardInterrupt("graceful shutdown interrupted")
        return request(
            current,
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,  # type: ignore[arg-type]
        )

    def cleanup_then_report_ordinary(
        *args: object,
        **kwargs: object,
    ) -> list[BaseException]:
        errors = drive_cleanup(*args, **kwargs)
        assert errors == []
        return [ordinary_error]

    monkeypatch.setattr(
        lsp_protocol.LspProtocol,
        "request",
        interrupt_shutdown_request,
    )
    monkeypatch.setattr(lsp_process, "_drive_cleanup", cleanup_then_report_ordinary)

    with pytest.raises(
        KeyboardInterrupt,
        match="graceful shutdown interrupted",
    ) as raised:
        process.shutdown(time.monotonic() + 5)

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.__cause__ is ordinary_error
    assert not lsp_process._coordinator_has_ownership(process._coordinator)
    assert not process.owner_root.exists()


def test_shutdown_unwraps_graceful_interruption_without_exception_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    protocol = process.protocol
    request = lsp_protocol.LspProtocol.request
    drive_cleanup = lsp_process._drive_cleanup
    ordinary_error = RuntimeError("ordinary wrapped shutdown cleanup error")

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("wrapped graceful shutdown interruption")
        except KeyboardInterrupt as error:
            return error

    interruption = make_interruption()
    wrapper = RuntimeError("graceful shutdown wrapper")
    wrapper.__cause__ = interruption

    def wrapped_shutdown_request(
        current: lsp_protocol.LspProtocol,
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: object = None,
    ) -> object:
        if current is protocol and method == "shutdown":
            raise wrapper
        return request(
            current,
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,  # type: ignore[arg-type]
        )

    def cleanup_then_report_ordinary(
        *args: object,
        **kwargs: object,
    ) -> list[BaseException]:
        errors = drive_cleanup(*args, **kwargs)
        assert errors == []
        return [ordinary_error]

    monkeypatch.setattr(
        lsp_protocol.LspProtocol,
        "request",
        wrapped_shutdown_request,
    )
    monkeypatch.setattr(lsp_process, "_drive_cleanup", cleanup_then_report_ordinary)

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped graceful shutdown interruption",
    ) as raised:
        process.shutdown(time.monotonic() + 5)

    assert raised.value is interruption
    assert raised.value.__cause__ is ordinary_error
    assert raised.value.__context__ is None
    assert wrapper.__cause__ is interruption
    assert not lsp_process._coordinator_has_ownership(process._coordinator)
    assert not process.owner_root.exists()


def test_shutdown_later_cleanup_interruption_outranks_first_ordinary_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    drive_cleanup = lsp_process._drive_cleanup
    ordinary_error = RuntimeError("first ordinary shutdown cleanup error")

    def make_interruption() -> SystemExit:
        try:
            raise SystemExit(43)
        except SystemExit as error:
            return error

    interruption = make_interruption()

    def cleanup_then_return_errors(
        *args: object,
        **kwargs: object,
    ) -> list[BaseException]:
        errors = drive_cleanup(*args, **kwargs)
        assert errors == []
        return [ordinary_error, interruption]

    monkeypatch.setattr(lsp_process, "_drive_cleanup", cleanup_then_return_errors)

    with pytest.raises(SystemExit) as raised:
        process.shutdown(time.monotonic() + 5)

    assert raised.value is interruption
    assert raised.value.code == 43
    assert raised.value.__cause__ is ordinary_error
    traceback_names: list[str] = []
    current = raised.value.__traceback__
    while current is not None:
        traceback_names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    assert "make_interruption" in traceback_names
    assert not lsp_process._coordinator_has_ownership(process._coordinator)
    assert not process.owner_root.exists()


def test_fatal_request_restarts_once_with_fresh_generation(tmp_path: Path) -> None:
    marker = tmp_path / "crashed"
    process = _start(tmp_path, "--lifecycle", "--crash-once-marker", str(marker))
    first_generation = process.generation_nonce

    assert process.request("echo", {"ok": True}, deadline=time.monotonic() + 5) == {
        "ok": True
    }
    assert process.restart_count == 1
    assert process.generation_nonce != first_generation
    process.close(time.monotonic() + 5)


def test_generation_change_wait_returns_false_for_every_terminal_state(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    cases = (
        (lsp_process._LifecyclePhase.RUNNING, "success"),
        (lsp_process._LifecyclePhase.STOPPING_SUCCESS, None),
        (lsp_process._LifecyclePhase.STOPPING_FAILURE, None),
        (lsp_process._LifecyclePhase.CLEANUP_PENDING, None),
        (lsp_process._LifecyclePhase.STOPPED_SUCCESS, None),
        (lsp_process._LifecyclePhase.STOPPED_FAILURE, None),
    )
    try:
        for phase, terminal_outcome in cases:
            with coordinator.condition:
                coordinator.phase = phase
                coordinator.terminal_outcome = terminal_outcome
            assert lsp_process._wait_for_generation_change(
                process,
                process.generation_nonce,
                time.monotonic() + 0.02,
            ) == (
                False,
                None,
            )
    finally:
        with coordinator.condition:
            coordinator.phase = lsp_process._LifecyclePhase.RUNNING
            coordinator.terminal_outcome = None
        process.close(time.monotonic() + 5)


def test_second_fatal_failure_is_terminal_and_retains_bounded_evidence(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--always-crash")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    assert recovery is not None and heartbeat is not None

    try:
        with pytest.raises(ProtocolViolation):
            process.request("echo", {}, deadline=time.monotonic() + 5)

        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(1)
        heartbeat.join(1)
        assert not recovery.is_alive()
        assert not heartbeat.is_alive()
        assert process.restart_count == 1
        assert process.state is ProcessState.FAILED
        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        assert set(failure) == {
            "code",
            "generation_nonce",
            "owner_nonce",
            "server_pid",
            "timestamp",
        }
        assert failure["code"] == "process_exited"
        assert not (process.owner_root / "lease.json").exists()
        assert coordinator.active is None
        assert coordinator.candidate is None
        assert coordinator.retired == []
        assert coordinator.owner_directory is None
        assert coordinator.recovery_thread is None
        assert coordinator.heartbeat_thread is None
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle release")
def test_windows_terminal_failure_releases_retained_process_handle(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    retained = process.process
    handle = int(retained._handle)
    assert _windows_handle_status(handle) == (True, 0)

    process._terminal_failure("injected_failure", time.monotonic() + 5)

    assert process.state is ProcessState.FAILED
    assert process._coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
    assert _windows_handle_status(handle) == (False, 6)
    assert retained.returncode is not None
    assert retained.poll() == retained.returncode


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle release")
def test_windows_restart_releases_old_generation_process_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    old = process.process
    old_handle = int(old._handle)
    close_observations: list[tuple[bool, int]] = []
    real_close = lsp_process._lsp_process_tree._close_windows_process_handle

    def close_process_handle(child: subprocess.Popen[bytes]) -> None:
        real_close(child)
        if child is old:
            close_observations.append(_windows_handle_status(old_handle))

    monkeypatch.setattr(
        lsp_process._lsp_process_tree,
        "_close_windows_process_handle",
        close_process_handle,
    )
    assert _windows_handle_status(old_handle) == (True, 0)

    try:
        process.restart(time.monotonic() + 5)

        assert process.restart_count == 1
        assert process.process is not old
        assert close_observations == [(False, 6)]
        assert old._handle.closed is True
        assert old.returncode is not None
        assert old.poll() == old.returncode
    finally:
        process.close(time.monotonic() + 5)


def test_observed_second_generation_exit_dominates_overlapping_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    process.restart(time.monotonic() + 5)
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.protocol is not None
    protocol = generation.protocol
    server_pid = process.process.pid
    assert process.restart_count == 1
    lsp_process._stop_recovery_owner(coordinator, time.monotonic() + 2)

    exit_observed = threading.Event()
    release_callbacks = threading.Event()
    accepted_intents: list[str] = []
    shutdown_errors: list[BaseException] = []
    real_queue_failure = lsp_process._queue_generation_failure
    real_enqueue = lsp_process._enqueue_failure_intent
    real_become_fatal = protocol._become_fatal

    def gated_queue_failure(
        current: lsp_process._LifecycleCoordinator,
        target: lsp_process._Generation,
        reason: str,
    ) -> bool:
        if current is coordinator and target is generation:
            if threading.current_thread() is generation.exit_thread:
                exit_observed.set()
            assert release_callbacks.wait(5)
        return real_queue_failure(current, target, reason)

    def gated_become_fatal(
        reason: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        if threading.current_thread() is generation.exit_thread:
            exit_observed.set()
        assert release_callbacks.wait(5)
        real_become_fatal(reason, cause=cause)

    def record_enqueue(
        current: lsp_process._LifecycleCoordinator,
        intent: lsp_process._FailureIntent,
    ) -> bool:
        accepted = real_enqueue(current, intent)
        if (
            accepted
            and current is coordinator
            and intent.generation_nonce == generation.nonce
        ):
            accepted_intents.append(intent.generation_nonce)
        return accepted

    def shutdown() -> None:
        try:
            process.shutdown(time.monotonic() + 5)
        except BaseException as error:
            shutdown_errors.append(error)

    monkeypatch.setattr(lsp_process, "_queue_generation_failure", gated_queue_failure)
    monkeypatch.setattr(lsp_process, "_enqueue_failure_intent", record_enqueue)
    monkeypatch.setattr(protocol, "_become_fatal", gated_become_fatal)
    closer = threading.Thread(target=shutdown)
    try:
        process.process.kill()
        assert exit_observed.wait(3)
        closer.start()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase
            in {
                lsp_process._LifecyclePhase.STOPPING_SUCCESS,
                lsp_process._LifecyclePhase.STOPPING_FAILURE,
                lsp_process._LifecyclePhase.CLEANUP_PENDING,
                lsp_process._LifecyclePhase.STOPPED_SUCCESS,
                lsp_process._LifecyclePhase.STOPPED_FAILURE,
            },
        )
        release_callbacks.set()
        closer.join(5)

        assert not closer.is_alive()
        assert shutdown_errors == []
        assert accepted_intents == [generation.nonce]
        assert process.restart_count == 1
        assert process.state is ProcessState.FAILED
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        lsp_process._validate_failure_record(
            failure,
            code="process_exited",
            owner_nonce=process.owner_nonce,
            generation_nonce=generation.nonce,
            pid=server_pid,
        )
        assert _owner_entry_names(process) == {
            "cancellation",
            "failure.json",
            "owner.json",
        }
    finally:
        release_callbacks.set()
        _join_started(closer)
        _close_if_owned(process, coordinator)


def test_expected_second_generation_exit_precedes_death_and_shutdown_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    process.restart(time.monotonic() + 5)
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.protocol is not None
    protocol = generation.protocol
    assert process.restart_count == 1
    lsp_process._stop_recovery_owner(coordinator, time.monotonic() + 2)

    expected_exit_marked = threading.Event()
    release_shutdown_request = threading.Event()
    accepted_intents: list[str] = []
    shutdown_errors: list[BaseException] = []
    real_request = protocol.request
    real_enqueue = lsp_process._enqueue_failure_intent

    def gated_request(
        method: str,
        params: object,
        *,
        deadline: float,
        cancellation: lsp_protocol.CancellationToken | None = None,
    ) -> object:
        if method == "shutdown":
            assert generation.expected_exit.is_set()
            expected_exit_marked.set()
            assert release_shutdown_request.wait(5)
        return real_request(
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,
        )

    def record_enqueue(
        current: lsp_process._LifecycleCoordinator,
        intent: lsp_process._FailureIntent,
    ) -> bool:
        accepted = real_enqueue(current, intent)
        if (
            accepted
            and current is coordinator
            and intent.generation_nonce == generation.nonce
        ):
            accepted_intents.append(intent.generation_nonce)
        return accepted

    def shutdown() -> None:
        try:
            process.shutdown(time.monotonic() + 5)
        except BaseException as error:
            shutdown_errors.append(error)

    monkeypatch.setattr(protocol, "request", gated_request)
    monkeypatch.setattr(lsp_process, "_enqueue_failure_intent", record_enqueue)
    closer = threading.Thread(target=shutdown)
    try:
        closer.start()
        assert expected_exit_marked.wait(3)
        process.process.kill()
        process.process.wait(timeout=3)
        release_shutdown_request.set()
        closer.join(5)

        assert not closer.is_alive()
        assert shutdown_errors == []
        assert accepted_intents == []
        assert process.restart_count == 1
        assert coordinator.terminal_outcome == "success"
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_SUCCESS
        assert not process.owner_root.exists()
    finally:
        release_shutdown_request.set()
        if closer.ident is not None:
            closer.join(5)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_idle_fatal_restarts_once_then_second_idle_fatal_is_terminal(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "idle-exit-marker"
    gate_prefix = tmp_path / "idle-exit-gate"
    first_gate = Path(f"{gate_prefix}.first")
    second_gate = Path(f"{gate_prefix}.second")
    process = _start(
        tmp_path,
        "--lifecycle",
        "--idle-exit-marker",
        str(marker),
        "--idle-exit-gate-prefix",
        str(gate_prefix),
    )
    try:
        first_generation = process.generation_nonce
        first_gate.write_bytes(b"")
        deadline = time.monotonic() + 3

        lease_path = process.owner_root / "lease.json"
        _await_restart_with_lease(process, lease_path, deadline)

        assert process.restart_count == 1
        assert process.generation_nonce != first_generation
        lease = json.loads(lease_path.read_bytes())
        assert lease["generation_nonce"] == process.generation_nonce
        assert lease["server_pid"] == process.process.pid

        second_gate.write_bytes(b"")
        assert _coordinator_wait(
            process,
            lambda: process._coordinator.phase
            is lsp_process._LifecyclePhase.STOPPED_FAILURE,
        )
        assert process.state is ProcessState.FAILED
        assert process._tree is None
        assert (process.owner_root / "failure.json").is_file()
        assert not lease_path.exists()
    finally:
        first_gate.touch(exist_ok=True)
        second_gate.touch(exist_ok=True)
        if lsp_process._coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.parametrize("ending", ["crash", "timeout"])
def test_fatal_endings_leave_no_real_descendants_after_leader_exit(
    tmp_path: Path, ending: str
) -> None:
    pid_log = tmp_path / f"{ending}-descendants.txt"
    arguments = [
        "--lifecycle",
        "--spawn-descendant",
        "--descendant-pid-log",
        str(pid_log),
    ]
    arguments.append("--always-crash" if ending == "crash" else "--hang-then-exit")
    process = _start(tmp_path, *arguments)

    if ending == "crash":
        with pytest.raises(ProtocolViolation):
            process.request("ending", {}, deadline=time.monotonic() + 5)
        _await_settled_failure(process)
    else:
        with pytest.raises(TimeoutError):
            process.request("ending", {}, deadline=time.monotonic() + 0.05)
        _await_restart(process)
        assert process.restart_count == 1
        process.shutdown(time.monotonic() + 5)

    pids = [int(value) for value in pid_log.read_text(encoding="ascii").splitlines()]
    assert len(pids) == 2
    _wait_for_owned_pids_to_exit(process, pids)
    process.close(time.monotonic() + 5)


def test_application_error_and_caller_cancellation_never_restart(
    tmp_path: Path,
) -> None:
    application = LspProcess.start(
        _command("--lifecycle", "--application-error"),
        cwd=tmp_path,
        owner_root=tmp_path / ("d" * 32),
    )
    with pytest.raises(JsonRpcResponseError):
        application.request("echo", {}, deadline=time.monotonic() + 2)
    assert application.restart_count == 0
    application.close(time.monotonic() + 5)

    marker = tmp_path / "cancel-hung"
    cancelled = LspProcess.start(
        _command("--lifecycle", "--hang-once-marker", str(marker)),
        cwd=tmp_path,
        owner_root=tmp_path / ("e" * 32),
    )
    source = CancellationSource()
    timer = threading.Timer(0.05, source.cancel)
    timer.start()
    try:
        with pytest.raises(RequestCancelled):
            cancelled.request(
                "slow",
                {},
                deadline=time.monotonic() + 2,
                cancellation=source.token,
            )
        assert cancelled.restart_count == 0
    finally:
        timer.join()
        cancelled.close(time.monotonic() + 5)


def test_cancel_all_ignores_terminal_failure_before_evidence_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    cancellations: list[str] = []
    monkeypatch.setattr(process.protocol, "cancel_all", cancellations.append)

    lsp_process._mark_terminal_failure(
        process,
        coordinator,
        "injected_failure",
        time.monotonic() + 1,
    )
    try:
        assert process.state is ProcessState.DEGRADED
        process.cancel_all("too late")
        assert cancellations == []
    finally:
        process.close(time.monotonic() + 5)


def test_expired_drain_restarts_without_request_traffic(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "hung"
    process = _start(tmp_path, "--lifecycle", "--hang-once-marker", str(marker))
    first_generation = process.generation_nonce

    try:
        with pytest.raises(TimeoutError):
            process.request("slow", {}, deadline=time.monotonic() + 0.05)
        assert process.restart_count == 0
        assert _coordinator_wait(process, lambda: process.restart_count == 1, timeout=3)

        assert process.restart_count == 1
        assert process.generation_nonce != first_generation
        assert process.request(
            "echo", {"fresh": True}, deadline=time.monotonic() + 5
        ) == {"fresh": True}
        assert process.restart_count == 1
    finally:
        process.close(time.monotonic() + 5)


def test_cancel_all_releases_request_then_close_cleans_descendant_and_threads(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "hung"
    pid_file = tmp_path / "descendant.pid"
    process = _start(
        tmp_path,
        "--lifecycle",
        "--hang-once-marker",
        str(marker),
        "--spawn-descendant",
        "--descendant-pid-file",
        str(pid_file),
    )
    descendant = _await_descendant_pid(pid_file)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            process.request, "slow", {}, deadline=time.monotonic() + 5
        )
        _await_pending_request(process)
        process.cancel_all("manager cancellation")
        with pytest.raises(RequestCancelled):
            pending.result(timeout=1)

    thread_names = {
        process.protocol.reader_thread.name,
        process.protocol.writer_thread.name,
        process._stderr_thread.name,
        process._exit_thread.name,
        process._heartbeat_thread.name,
    }
    process.close(time.monotonic() + 5)

    assert not _pid_alive(descendant)
    assert not any(thread.name in thread_names for thread in threading.enumerate())


def test_normal_interpreter_exit_runs_bounded_atexit_tree_cleanup(tmp_path: Path) -> None:
    owner = tmp_path / ("c" * 32)
    pid_file = tmp_path / "atexit-descendant.pid"
    code = (
        "import sys; from pathlib import Path; from lsp_process import LspProcess; "
        "LspProcess.start(sys.argv[1:7], cwd=Path(sys.argv[7]), "
        "owner_root=Path(sys.argv[8]))"
    )
    command = _command(
        "--lifecycle",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(pid_file),
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(lsp_process.__file__).resolve().parent)

    completed = subprocess.run(
        [sys.executable, "-c", code, *command, str(tmp_path), str(owner)],
        cwd=tmp_path,
        env=environment,
        shell=False,
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    descendant = int(pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while _pid_alive(descendant) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _pid_alive(descendant)
    assert not owner.exists()


def test_environment_is_sorted_explicit_and_excludes_credentials() -> None:
    source = {
        "WINDIR": r"C:\Windows",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "SSH_AUTH_SOCK": "secret",
        "NPM_TOKEN": "secret",
        "OPENAI_API_KEY": "secret",
        "PYTHONPATH": "secret",
        "NODE_OPTIONS": "secret",
        "AWS_PROFILE": "secret",
        "OPENCODE_CONFIG": "secret",
    }
    before = dict(source)

    environment = lsp_environment(source)

    assert list(environment) == sorted(environment)
    assert set(environment) <= LSP_ENV_ALLOWLIST
    assert environment == {
        name: source[name] for name in sorted(LSP_ENV_ALLOWLIST) if name in source
    }
    assert source == before


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ({1: "value"}, TypeError),
        ({"PATH": 1}, TypeError),
        ({"PATH\0BAD": "value"}, ValueError),
        ({"PATH": "bad\0value"}, ValueError),
    ],
)
def test_environment_rejects_non_strings_and_nuls_before_spawn(
    source: dict[object, object], error: type[Exception]
) -> None:
    if os.name == "nt":
        source["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with pytest.raises(error):
        lsp_environment(source)  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "nt", reason="Windows process environment contract")
def test_environment_requires_inherited_systemroot_on_windows() -> None:
    with pytest.raises(ValueError, match="SYSTEMROOT"):
        lsp_environment({"PATH": os.environ.get("PATH", "")})
    with pytest.raises(ValueError, match="SYSTEMROOT"):
        lsp_environment({"SYSTEMROOT": "relative-system-root"})


def test_child_receives_only_the_allowlisted_environment(tmp_path: Path) -> None:
    process = _start(tmp_path, "--report-environment")
    _expect_active_generation_exit(process)
    environment = process.request("environment", {}, deadline=time.monotonic() + 5)
    assert isinstance(environment, dict)
    # CoreFoundation adds `__CF_USER_TEXT_ENCODING` to a macOS process itself,
    # after exec and outside the environment the parent passed.
    assert set(environment) - _OS_INJECTED_ENVIRONMENT <= LSP_ENV_ALLOWLIST
    assert not any(
        name.startswith("PYTHON") or name in {"NODE_OPTIONS", "OPENAI_API_KEY"}
        for name in environment
    )
    process.close(time.monotonic() + 5)


def test_popen_uses_exact_safe_binary_pipe_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    real_popen = subprocess.Popen

    def spy(args: object, **kwargs: object):
        calls.append((args, kwargs))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(lsp_process.subprocess, "Popen", spy)
    process = _start(tmp_path, "--lifecycle")
    process.close(time.monotonic() + 5)

    assert len(calls) == 1
    arguments, options = calls[0]
    assert isinstance(arguments, list)
    assert Path(arguments[0]).is_absolute()
    expected = {
        "cwd": tmp_path.resolve(),
        "env": lsp_environment(),
        "shell": False,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }
    if os.name == "nt":
        expected["creationflags"] = 0x00000004
    else:
        expected["start_new_session"] = True
    assert options == expected


def test_stderr_ring_retains_exact_last_four_mib_across_odd_chunks(tmp_path: Path) -> None:
    total = 5 * 1024 * 1024 + 17
    process = _start(tmp_path, "--stderr-bytes", str(total))
    _expect_active_generation_exit(process)
    _wait(process, 15)

    expected = bytes(index % 251 for index in range(total - MAX_STDERR_BYTES, total))
    assert process.stderr_bytes() == expected
    assert len(process.stderr_bytes()) == MAX_STDERR_BYTES
    process.close(time.monotonic() + 5)


def test_zero_stderr_and_concurrent_snapshots_never_block(tmp_path: Path) -> None:
    process = _start(tmp_path, "--sleep-seconds", "2")
    _expect_active_generation_exit(process)
    stop = threading.Event()

    def snapshot() -> None:
        while not stop.is_set():
            assert len(process.stderr_bytes()) <= MAX_STDERR_BYTES

    threads = [threading.Thread(target=snapshot) for _ in range(4)]
    for thread in threads:
        thread.start()
    _wait(process)
    stop.set()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert process.stderr_bytes() == b""
    process.close(time.monotonic() + 5)


def test_each_start_has_independent_lowercase_hex_nonces(tmp_path: Path) -> None:
    first = LspProcess.start(
        _command("--sleep-seconds", "2"),
        cwd=tmp_path,
        owner_root=tmp_path / ("a" * 32),
    )
    _expect_active_generation_exit(first)
    second = LspProcess.start(
        _command("--sleep-seconds", "2"),
        cwd=tmp_path,
        owner_root=tmp_path / ("b" * 32),
    )
    _expect_active_generation_exit(second)
    _wait(first)
    _wait(second)
    for process in (first, second):
        recovery = process._recovery_thread
        if recovery is not None:
            recovery.join(3)
    for nonce in (
        first.owner_nonce,
        first.generation_nonce,
        second.owner_nonce,
        second.generation_nonce,
    ):
        assert len(nonce) == 32
        assert nonce == nonce.lower()
        int(nonce, 16)
    assert first.owner_nonce == "a" * 32
    assert second.owner_nonce == "b" * 32
    assert first.generation_nonce != second.generation_nonce
    assert first.protocol.generation_nonce == first.generation_nonce
    first.close(time.monotonic() + 5)
    second.close(time.monotonic() + 5)


def test_owner_json_is_canonical_redacted_restricted_and_has_only_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_argument = str(tmp_path / "repository-secret-path" / "credential-value")
    environment_secret = "allowlisted-environment-secret-7f31"
    monkeypatch.setenv("TMP", environment_secret)
    process = _start(
        tmp_path, "--ignored-secret", secret_argument, "--sleep-seconds", "2"
    )
    owner_file = process.owner_root / "owner.json"
    raw = owner_file.read_bytes()
    record = json.loads(raw)

    assert raw == json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    assert set(record) == {
        "command_basename",
        "generation_nonce",
        "owner_nonce",
        "owner_pid",
        "started_at",
        "state",
    }
    assert record["owner_pid"] == process.process.pid
    assert record["owner_nonce"] == process.owner_nonce
    assert record["generation_nonce"] == process.generation_nonce
    assert record["command_basename"] == Path(process.process.args[0]).name
    assert record["state"] == "process_running"
    persisted = raw.decode()
    assert secret_argument not in persisted
    assert environment_secret not in persisted
    assert str(tmp_path.resolve()) not in persisted
    assert str(Path(sys.executable).resolve()) not in persisted
    assert set(path.name for path in process.owner_root.iterdir()) == {
        "cancellation",
        "lease.json",
        "owner.json",
    }
    if os.name == "nt":
        _assert_lsp_acl_is_owner_only(process.owner_root, inherited=False)
        _assert_lsp_acl_is_owner_only(
            process.owner_root / "cancellation", inherited=True
        )
        _assert_lsp_acl_is_owner_only(owner_file, inherited=True)
        _assert_lsp_acl_is_owner_only(process.owner_root / "lease.json", inherited=True)
    else:
        compile_cache._verify_owner_only(process.owner_root, 0o700)
        compile_cache._verify_owner_only(process.owner_root / "cancellation", 0o700)
        compile_cache._verify_owner_only(owner_file, 0o600)
        compile_cache._verify_owner_only(process.owner_root / "lease.json", 0o600)
        assert stat.S_IMODE(process.owner_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((process.owner_root / "cancellation").stat().st_mode) == 0o700
        assert stat.S_IMODE(owner_file.stat().st_mode) == 0o600
    process.close(time.monotonic() + 5)


@pytest.mark.parametrize("deadline", [math.nan, math.inf, "later", True])
def test_wait_for_exit_rejects_invalid_deadline(tmp_path: Path, deadline: object) -> None:
    process = _start(tmp_path, "--sleep-seconds", "2")
    with pytest.raises((TypeError, ValueError)):
        process.wait_for_exit(deadline)  # type: ignore[arg-type]
    _expect_active_generation_exit(process)
    _wait(process)
    process.close(time.monotonic() + 5)


def test_wait_for_exit_timeout_does_not_kill_process(tmp_path: Path) -> None:
    process = _start(tmp_path, "--exit-while-pending")
    with pytest.raises(TimeoutError):
        process.wait_for_exit(time.monotonic() - 1)
    assert process.process.poll() is None
    _expect_active_generation_exit(process)
    process.process.stdin.close()
    _wait(process)
    process.close(time.monotonic() + 5)


def test_request_delegates_token_and_updates_last_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--echo")
    _expect_active_generation_exit(process)
    source = CancellationSource()
    observed: list[tuple[object, object, float, object]] = []
    real_request = process.protocol.request

    def spy(method: object, params: object, *, deadline: float, cancellation: object = None):
        observed.append((method, params, deadline, cancellation))
        return real_request(method, params, deadline=deadline, cancellation=cancellation)

    monkeypatch.setattr(process.protocol, "request", spy)
    before = process.last_used_monotonic
    deadline = time.monotonic() + 5
    assert process.request("echo", {"safe": True}, deadline=deadline, cancellation=source.token) == {
        "safe": True
    }
    assert observed == [("echo", {"safe": True}, deadline, source.token)]
    assert process.last_used_monotonic >= before
    _wait(process)
    process.close(time.monotonic() + 5)


def test_exit_monitor_fails_all_pending_once_and_marks_failed(tmp_path: Path) -> None:
    process = _start(tmp_path, "--exit-while-pending")
    callbacks: list[str] = []
    original = process.protocol._fatal_callback
    process.protocol._fatal_callback = lambda reason: (callbacks.append(reason), original(reason))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                process.request,
                "pending",
                {},
                deadline=time.monotonic() + 5,
            )
            for _ in range(2)
        ]
        for future in futures:
            with pytest.raises(ProtocolViolation):
                future.result(timeout=5)

    _wait(process)
    assert _coordinator_wait(
        process,
        lambda: process._coordinator.phase
        is lsp_process._LifecyclePhase.STOPPED_FAILURE,
    )
    assert process.state is ProcessState.FAILED
    assert len(callbacks) == 1
    assert process.protocol.pending_count == 0
    assert json.loads((process.owner_root / "owner.json").read_bytes())["state"] == (
        "process_running"
    )
    assert json.loads((process.owner_root / "failure.json").read_bytes())["code"] == (
        "process_exited"
    )
    with pytest.raises(RuntimeError, match="exited"):
        process.request("later", {}, deadline=time.monotonic() + 1)
    process.close(time.monotonic() + 5)


@pytest.mark.parametrize("command", [[], [""], ["bad\0command"], "not-a-sequence"])
def test_start_rejects_invalid_commands_before_creating_owner(
    tmp_path: Path, command: object
) -> None:
    owner = tmp_path / OWNER_NONCE
    with pytest.raises((TypeError, ValueError, FileNotFoundError)):
        LspProcess.start(command, cwd=tmp_path, owner_root=owner)  # type: ignore[arg-type]
    assert not owner.exists()


def test_start_rejects_invalid_cwd_and_preexisting_owner(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(ValueError, match="cwd"):
        LspProcess.start(_command(), cwd=missing, owner_root=owner)
    owner.mkdir()
    sentinel = owner / "preexisting.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_start_rejects_symlink_owner(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    owner = tmp_path / OWNER_NONCE
    try:
        owner.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(FileExistsError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)


def test_startup_failure_terminates_child_and_retains_bounded_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    class BrokenProtocol:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="protocol startup failed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=5)
    assert children[0].poll() is not None
    if os.name == "nt":
        assert _windows_handle_status(int(children[0]._handle)) == (False, 6)
    assert set(path.name for path in owner.iterdir()) == {
        "cancellation",
        "failure.json",
        "owner.json",
    }
    assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_stderr_thread_start_failure_retains_evidence_without_masking_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_start = threading.Thread.start

    def fail_stderr_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stderr-"):
            raise RuntimeError("stderr thread start failed")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_stderr_start)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="stderr thread start failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert owner.is_dir()
    assert not (owner / "owner.json").exists()
    failure = json.loads((owner / "failure.json").read_bytes())
    assert failure["code"] == "startup_failed"
    assert "server_pid" in failure


def test_startup_owner_is_registered_before_first_owned_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    create = lsp_process._OwnerDirectory.create
    registered: list[bool] = []

    def observe_create(owner: lsp_process._OwnerDirectory, deadline: float) -> None:
        registered.append(
            any(
                coordinator.owner_directory is owner
                for coordinator in lsp_process._pending_startup_cleanup_snapshot()
            )
        )
        create(owner, deadline)

    monkeypatch.setattr(lsp_process._OwnerDirectory, "create", observe_create)
    process = _start(tmp_path, "--lifecycle")
    try:
        assert registered == [True]
        assert lsp_process._pending_startup_cleanup_snapshot() == ()
    finally:
        process.close(time.monotonic() + 5)


def test_startup_cleanup_rethrows_interruption_and_retains_registry_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[lsp_process._LifecycleCoordinator] = []
    real_drive_cleanup = lsp_process._drive_cleanup
    cleanup_calls = 0

    def fail_after_ownership(
        instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        captured.append(instance._coordinator)
        raise RuntimeError("startup failed after ownership")

    def interrupt_cleanup(*_args: object, **_kwargs: object) -> list[BaseException]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise KeyboardInterrupt("startup cleanup interrupted")
        return real_drive_cleanup(*_args, **_kwargs)

    monkeypatch.setattr(lsp_process, "_start_lifecycle_workers", fail_after_ownership)
    monkeypatch.setattr(lsp_process, "_drive_cleanup", interrupt_cleanup)

    coordinator: lsp_process._LifecycleCoordinator | None = None
    try:
        with pytest.raises(KeyboardInterrupt, match="startup cleanup interrupted"):
            _start(tmp_path, "--lifecycle")

        assert len(captured) == 1
        coordinator = captured[0]
        assert lsp_process._coordinator_has_ownership(coordinator)
        assert coordinator in lsp_process._pending_startup_cleanup_snapshot()
    finally:
        if captured:
            coordinator = captured[0]
            if lsp_process._coordinator_has_ownership(coordinator):
                lsp_process._retry_startup_cleanup(
                    coordinator,
                    time.monotonic() + 5,
                )
    assert coordinator is not None
    assert not lsp_process._coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process._pending_startup_cleanup_snapshot()


def test_startup_cleanup_rethrows_returned_interruption_after_retaining_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[lsp_process._LifecycleCoordinator] = []
    real_drive_cleanup = lsp_process._drive_cleanup
    cleanup_calls = 0

    def fail_after_ownership(
        instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        captured.append(instance._coordinator)
        raise RuntimeError("startup failed after ownership")

    def make_interruption() -> KeyboardInterrupt:
        try:
            raise KeyboardInterrupt("returned startup cleanup interruption")
        except KeyboardInterrupt as interruption:
            return interruption

    interruption = make_interruption()

    def return_interruption(*args: object, **kwargs: object) -> list[BaseException]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            return [interruption]
        return real_drive_cleanup(*args, **kwargs)

    monkeypatch.setattr(lsp_process, "_start_lifecycle_workers", fail_after_ownership)
    monkeypatch.setattr(lsp_process, "_drive_cleanup", return_interruption)

    coordinator: lsp_process._LifecycleCoordinator | None = None
    try:
        with pytest.raises(
            KeyboardInterrupt,
            match="returned startup cleanup interruption",
        ) as raised:
            _start(tmp_path, "--lifecycle")

        assert raised.value is interruption
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert str(raised.value.__cause__) == "startup failed after ownership"
        traceback_names: list[str] = []
        current = raised.value.__traceback__
        while current is not None:
            traceback_names.append(current.tb_frame.f_code.co_name)
            current = current.tb_next
        assert "make_interruption" in traceback_names
        assert len(captured) == 1
        coordinator = captured[0]
        assert lsp_process._coordinator_has_ownership(coordinator)
        assert coordinator in lsp_process._pending_startup_cleanup_snapshot()
    finally:
        if captured:
            coordinator = captured[0]
            if lsp_process._coordinator_has_ownership(coordinator):
                lsp_process._retry_startup_cleanup(
                    coordinator,
                    time.monotonic() + 5,
                )
    assert coordinator is not None
    assert not lsp_process._coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process._pending_startup_cleanup_snapshot()


def _exercise_owner_open_failures_never_consume_startup_cleanup_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _pending_cleanup_ids()
    opened: list[Path] = []
    failures: list[BaseException] = []

    def fail_open(
        _cls: type[lsp_process._OwnerDirectory], owner_root: Path
    ) -> lsp_process._OwnerDirectory:
        opened.append(owner_root)
        raise OSError("owner directory open failed")

    def fail_cleanup(*_args: object, **_kwargs: object) -> list[BaseException]:
        raise RuntimeError("startup cleanup also failed")

    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "open",
        classmethod(fail_open),
    )
    monkeypatch.setattr(lsp_process, "_drive_cleanup", fail_cleanup)
    try:
        for index in range(lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1):
            _collect_start_failure(failures, tmp_path, index + 200)

        assert len(opened) == lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1
        assert len(failures) == len(opened)
        assert all(type(error) is OSError for error in failures)
        assert all(str(error) == "owner directory open failed" for error in failures)
        assert _pending_cleanup_ids() == baseline
    finally:
        _unregister_new_cleanup_owners(baseline)


def test_protocol_startup_cleanup_owner_is_adopted_for_coordinator_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RetainedProtocol:
        def __init__(self) -> None:
            self.released = False
            self.stop_calls = 0
            self.finish_calls = 0

        def _stop_io_for_process_cleanup(self) -> None:
            self.stop_calls += 1

        def _finish_io_after_process_exit(self, _deadline: float) -> None:
            self.finish_calls += 1
            if not self.released:
                raise TimeoutError("protocol startup owner blocked")

    retained = RetainedProtocol()

    def fail_protocol(*_args: object, **_kwargs: object) -> None:
        error = lsp_process._ProtocolStartupCleanupError(
            retained,  # type: ignore[arg-type]
            (TimeoutError("protocol startup owner blocked"),),
        )
        raise error from RuntimeError("protocol startup failed")

    monkeypatch.setattr(lsp_process, "LspProtocol", fail_protocol)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(lsp_process.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    coordinator = raised.value.coordinator
    assert coordinator is not None
    generations = [
        coordinator.active,
        coordinator.candidate,
        *coordinator.retired,
    ]
    assert any(
        generation is not None and generation.protocol is retained
        for generation in generations
    )
    assert retained.stop_calls == 1
    assert retained.finish_calls == 1
    assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
    assert lsp_process._coordinator_has_ownership(coordinator)
    assert coordinator in lsp_process._pending_startup_cleanup_snapshot()

    retained.released = True
    raised.value.retry_cleanup(time.monotonic() + 5)
    assert not lsp_process._coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process._pending_startup_cleanup_snapshot()


def test_released_startup_failures_do_not_exhaust_cleanup_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[lsp_process._LifecycleCoordinator] = []
    failures: list[BaseException] = []

    def fail_workers(
        instance: LspProcess,
        _deadline: float | None = None,
    ) -> None:
        coordinator = instance._coordinator
        protocol = instance.protocol
        stop_io = protocol._stop_io_for_process_cleanup
        first = True

        def transient_stop_error() -> None:
            nonlocal first
            if first:
                first = False
                raise OSError("transient protocol stop failure")
            stop_io()

        captured.append(coordinator)
        monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", transient_stop_error)
        raise RuntimeError("startup worker failed after ownership was released")

    monkeypatch.setattr(lsp_process, "_start_lifecycle_workers", fail_workers)
    try:
        for index in range(lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1):
            owner = tmp_path / f"{index + 100:032x}"
            try:
                LspProcess.start(_command("--lifecycle"), cwd=tmp_path, owner_root=owner)
            except BaseException as error:
                failures.append(error)

        assert len(captured) == lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1
        assert len(failures) == len(captured)
        assert all(type(error) is RuntimeError for error in failures)
        assert _all_messages_equal(
            failures, "startup worker failed after ownership was released"
        )
        assert not _owned_coordinators(captured)
        assert lsp_process._pending_startup_cleanup_snapshot() == ()
    finally:
        _unregister_all(captured)
    _exercise_owner_open_failures_never_consume_startup_cleanup_registry(
        monkeypatch, tmp_path
    )


def test_startup_registry_atexit_hook_retries_owned_cleanup(tmp_path: Path) -> None:
    owner = lsp_process._OwnerDirectory.open(tmp_path / OWNER_NONCE)
    coordinator = lsp_process._LifecycleCoordinator(
        owner,
        startup_generation_nonce="b" * 32,
    )
    lsp_process._register_startup_cleanup(coordinator)
    owner.create(time.monotonic() + 1)

    lsp_process._atexit_cleanup_startups()

    failure = json.loads((owner.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "startup_failed"
    assert not lsp_process._coordinator_has_ownership(coordinator)
    assert coordinator not in lsp_process._pending_startup_cleanup_snapshot()


def test_startup_cleanup_registry_rejects_unbounded_ownership() -> None:
    coordinators = [
        lsp_process._LifecycleCoordinator(None)
        for _ in range(lsp_process._MAX_STARTUP_CLEANUP_OWNERS + 1)
    ]
    try:
        for coordinator in coordinators[:-1]:
            lsp_process._register_startup_cleanup(coordinator)
        with pytest.raises(RuntimeError, match="registry bound"):
            lsp_process._register_startup_cleanup(coordinators[-1])
    finally:
        for coordinator in coordinators:
            lsp_process._unregister_startup_cleanup(coordinator)


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-child Job ownership")
def test_process_startup_adopts_unattached_job_for_cleanup_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fault_enabled = True
    close_calls: list[int] = []

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        error = lsp_process._lsp_process_tree._ProcessTreeSpawnError(
            None,
            (OSError("job close failed"),),
            windows_job=37,
        )
        raise error from OSError("popen failed")

    def close_job(job: int) -> None:
        close_calls.append(job)
        if fault_enabled:
            raise OSError("job close failed")

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(lambda _cls, *args, **kwargs: fail_spawn(*args, **kwargs)),
    )
    monkeypatch.setattr(
        lsp_process._lsp_process_tree,
        "_close_windows_handle",
        close_job,
    )

    with pytest.raises(lsp_process.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE)

    coordinator = raised.value.coordinator
    assert coordinator is not None and coordinator.candidate is not None
    assert coordinator.candidate.windows_job == 37
    assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
    assert close_calls == [37]

    fault_enabled = False
    errors = lsp_process._drive_cleanup(
        None,
        time.monotonic() + 5,
        terminal=True,
        failure_code="startup_failed",
        coordinator_override=coordinator,
    )
    assert errors == []
    assert close_calls == [37, 37]
    assert not lsp_process._coordinator_has_ownership(coordinator)


@pytest.mark.parametrize(
    "name",
    ["owner", "A" * 32, "g" * 32, "a" * 31, "a" * 33],
)
def test_start_rejects_invalid_owner_identity_before_mutation(
    tmp_path: Path, name: str
) -> None:
    owner = tmp_path / name
    with pytest.raises(ValueError, match="owner_root"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_owner_identity_matches_caller_derived_root_not_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generation = "b" * 32
    real_token_hex = lsp_process.secrets.token_hex
    calls = 0

    def token_hex(size: int) -> str:
        nonlocal calls
        calls += 1
        return generation if calls == 1 else real_token_hex(size)

    monkeypatch.setattr(lsp_process.secrets, "token_hex", token_hex)
    process = _start(tmp_path, "--sleep-seconds", "2")
    assert process.owner_nonce == OWNER_NONCE
    assert process.generation_nonce == generation
    owner_record = json.loads((process.owner_root / "owner.json").read_bytes())
    assert owner_record["owner_nonce"] == OWNER_NONCE
    _expect_active_generation_exit(process)
    _wait(process)
    process.close(time.monotonic() + 5)


@pytest.mark.parametrize("generated", [RuntimeError("generation nonce failed"), "invalid"])
def test_generation_nonce_failure_occurs_before_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generated: object
) -> None:
    owner = tmp_path / OWNER_NONCE

    def fail_nonce(_size: int) -> str:
        if isinstance(generated, BaseException):
            raise generated
        assert isinstance(generated, str)
        return generated

    monkeypatch.setattr(lsp_process.secrets, "token_hex", fail_nonce)
    with pytest.raises((RuntimeError, ValueError), match="generation nonce"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_timestamp_failure_occurs_before_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE

    class BrokenDateTime:
        @staticmethod
        def now(_timezone: object) -> object:
            raise RuntimeError("timestamp failed")

    monkeypatch.setattr(lsp_process, "datetime", BrokenDateTime)
    with pytest.raises(RuntimeError, match="timestamp failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_popen_failure_retains_owner_root_and_omits_unknown_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    sibling = tmp_path / "preexisting.txt"
    sibling.write_text("preserve", encoding="utf-8")

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen failed")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", fail_popen)
    with pytest.raises(OSError, match="popen failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert set(path.name for path in owner.iterdir()) == {"cancellation", "failure.json"}
    failure = json.loads((owner / "failure.json").read_bytes())
    assert set(failure) == {
        "code",
        "generation_nonce",
        "owner_nonce",
        "timestamp",
    }
    assert failure["code"] == "startup_failed"
    assert sibling.read_text(encoding="utf-8") == "preserve"


def test_cancellation_directory_failure_retains_original_owner_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    if os.name == "nt":
        real_create = lsp_process._windows_workspace.create_directory

        def fail_cancellation(parent: int, name: str) -> int:
            if name == "cancellation":
                raise OSError("cancellation directory failed")
            return real_create(parent, name)

        monkeypatch.setattr(
            lsp_process._windows_workspace, "create_directory", fail_cancellation
        )
    else:
        real_mkdir = os.mkdir

        def fail_cancellation(
            path: object, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            if path == "cancellation":
                raise OSError("cancellation directory failed")
            real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(lsp_process.os, "mkdir", fail_cancellation)
    with pytest.raises(OSError, match="cancellation directory failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert owner.is_dir()
    assert set(path.name for path in owner.iterdir()) == {"failure.json"}


def test_owner_json_failure_terminates_child_and_retains_failure_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_owner_json(*_args: object, **_kwargs: object) -> None:
        raise OSError("owner JSON failed")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process, "_write_owner_record", fail_owner_json)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(OSError, match="owner JSON failed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=5)
    assert children[0].poll() is not None
    assert owner.is_dir()
    assert not (owner / "owner.json").exists()
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_exit_monitor_thread_start_failure_retains_process_owner_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_start = threading.Thread.start

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_exit_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-exit-"):
            raise RuntimeError("exit thread start failed")
        real_start(thread)

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(threading.Thread, "start", fail_exit_start)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="exit thread start failed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=5)
    assert children[0].poll() is not None
    assert owner.is_dir()
    assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"


def test_heartbeat_thread_start_failure_removes_live_lease_and_owned_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    }
    real_start = threading.Thread.start

    def fail_heartbeat_start(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-heartbeat-"):
            raise RuntimeError("heartbeat thread start failed")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_heartbeat_start)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(RuntimeError, match="heartbeat thread start failed"):
        LspProcess.start(
            _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
        )

    assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    assert not (owner / "lease.json").exists()
    assert {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    } == baseline


def test_owner_create_failure_uses_allocated_startup_generation_nonce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generation_nonce = "b" * 32
    owner_root = tmp_path / OWNER_NONCE
    real_create = lsp_process._OwnerDirectory.create

    def fail_after_create(
        owner: lsp_process._OwnerDirectory, deadline: float
    ) -> None:
        real_create(owner, deadline)
        raise RuntimeError("owner create failed after allocation")

    monkeypatch.setattr(lsp_process, "_new_generation_nonce", lambda: generation_nonce)
    monkeypatch.setattr(lsp_process._OwnerDirectory, "create", fail_after_create)

    with pytest.raises(RuntimeError, match="owner create failed after allocation"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner_root)

    failure = json.loads((owner_root / "failure.json").read_bytes())
    assert failure["generation_nonce"] == generation_nonce
    assert failure["owner_nonce"] == OWNER_NONCE
    assert failure["code"] == "startup_failed"


def test_recovery_cannot_run_before_startup_final_fence_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_start_workers = lsp_process._start_lifecycle_workers
    workers_started = threading.Event()
    release_startup = threading.Event()
    captured: list[LspProcess] = []
    start_errors: list[BaseException] = []

    def inject_fatal_before_final_fence(
        instance: LspProcess,
        deadline: float | None = None,
    ) -> None:
        real_start_workers(instance, deadline)
        captured.append(instance)
        instance.protocol._become_fatal("fatal during startup final fence")
        workers_started.set()
        assert release_startup.wait(3)

    def start() -> None:
        try:
            _start(tmp_path, "--lifecycle")
        except BaseException as error:
            start_errors.append(error)

    monkeypatch.setattr(
        lsp_process,
        "_start_lifecycle_workers",
        inject_fatal_before_final_fence,
    )
    start_thread = threading.Thread(target=start)
    start_thread.start()
    assert workers_started.wait(3)
    instance = captured[0]
    coordinator = instance._coordinator
    recovery_ran_before_final_fence = _coordinator_wait(
        instance,
        lambda: instance.restart_count != 0
        or coordinator.phase is not lsp_process._LifecyclePhase.RUNNING,
        timeout=0.2,
    )
    release_startup.set()
    start_thread.join(5)

    assert recovery_ran_before_final_fence is False
    assert not start_thread.is_alive()
    assert len(start_errors) == 1
    assert isinstance(start_errors[0], RuntimeError)
    assert json.loads((instance.owner_root / "failure.json").read_bytes())["code"] == (
        "startup_failed"
    )


def test_start_passes_one_absolute_deadline_to_owner_process_tree_and_protocol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deadlines: dict[str, float | None] = {}
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__
    real_protocol = lsp_process.LspProtocol
    real_create = lsp_process._OwnerDirectory.create

    def record_owner_create(
        owner: lsp_process._OwnerDirectory,
        deadline: float,
    ) -> None:
        deadlines["owner"] = deadline
        real_create(owner, deadline)

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process.ProcessTree:
        deadlines["spawn"] = deadline
        return real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]

    def record_protocol(*args: object, **kwargs: object) -> lsp_protocol.LspProtocol:
        deadlines["protocol"] = kwargs.get("_startup_deadline")  # type: ignore[assignment]
        return real_protocol(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    monkeypatch.setattr(lsp_process._OwnerDirectory, "create", record_owner_create)
    monkeypatch.setattr(lsp_process, "LspProtocol", record_protocol)
    process = _start(tmp_path, "--lifecycle")
    try:
        assert deadlines["owner"] == deadlines["spawn"]
        assert deadlines["protocol"] == deadlines["spawn"]
        assert deadlines["owner"] is not None
        assert deadlines["spawn"] is not None
    finally:
        process.close(time.monotonic() + 5)


def test_default_startup_deadline_begins_after_nonmutating_owner_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = [100.0]
    deadlines: dict[str, float] = {}
    real_open = lsp_process._OwnerDirectory.open.__func__
    real_create = lsp_process._OwnerDirectory.create

    def delayed_open(
        cls: type[lsp_process._OwnerDirectory],
        owner_root: Path,
    ) -> lsp_process._OwnerDirectory:
        owner = real_open(cls, owner_root)
        clock[0] += 10.0
        return owner

    def record_create(
        owner: lsp_process._OwnerDirectory,
        deadline: float,
    ) -> None:
        deadlines["create"] = deadline
        real_create(owner, deadline)

    def stop_after_mutation_budget_capture(
        _cls: type[lsp_process.ProcessTree],
        _command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process.ProcessTree:
        assert cwd == tmp_path.resolve()
        assert env
        deadlines["spawn"] = deadline
        raise RuntimeError("stopped after startup deadline capture")

    monkeypatch.setattr(lsp_process.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "open",
        classmethod(delayed_open),
    )
    monkeypatch.setattr(lsp_process._OwnerDirectory, "create", record_create)
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(stop_after_mutation_budget_capture),
    )

    with pytest.raises(RuntimeError, match="stopped after startup deadline capture"):
        LspProcess.start(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
        )

    expected = 110.0 + lsp_process._STARTUP_WAIT_SECONDS
    assert deadlines == {"create": expected, "spawn": expected}


def test_startup_heartbeat_stop_timeout_still_runs_all_other_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[lsp_process._LifecycleCoordinator] = []
    real_verify = lsp_process._OwnerDirectory.verify_lexical_identity
    checks = 0

    def fail_after_heartbeat(owner: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise RuntimeError("final startup fence failed")
        real_verify(owner)

    def fail_heartbeat_stop(
        coordinator: lsp_process._LifecycleCoordinator,
        _deadline: float,
        *,
        allow_expired: bool = False,
    ) -> None:
        del allow_expired
        captured.append(coordinator)
        raise TimeoutError("heartbeat stop blocked")

    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "verify_lexical_identity",
        fail_after_heartbeat,
    )
    monkeypatch.setattr(lsp_process, "_stop_heartbeat_owner", fail_heartbeat_stop)
    owner = tmp_path / OWNER_NONCE
    started = time.monotonic()

    try:
        with pytest.raises(lsp_process.StartupCleanupError) as raised:
            LspProcess.start(
                _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
            )

        assert time.monotonic() - started <= lsp_process._STARTUP_WAIT_SECONDS + 0.75
        assert len(captured) == 1
        assert isinstance(raised.value.__cause__, RuntimeError)
        coordinator = captured[0]
        assert _cleanup_error_types(coordinator) & {"TimeoutError"}
        assert not _generations_with_process(coordinator)
        assert coordinator.owner_directory is not None
        assert coordinator.owner_directory._closed is False
        assert (owner / "failure.json").is_file()
        assert (owner / "lease.json").is_file()
    finally:
        for coordinator in captured:
            _release_captured_coordinator(coordinator)


def test_restart_thread_start_failure_cleans_new_tree_and_becomes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    spawned: list[object] = []
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__
    real_start = threading.Thread.start

    def spawn(cls: object, *args: object, **kwargs: object) -> object:
        tree = real_spawn(cls, *args, **kwargs)
        spawned.append(tree)
        return tree

    def fail_restart_stderr(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stderr-"):
            raise RuntimeError("restart stderr thread start failed")
        real_start(thread)

    monkeypatch.setattr(
        lsp_process.ProcessTree, "_spawn_with_deadline", classmethod(spawn)
    )
    monkeypatch.setattr(threading.Thread, "start", fail_restart_stderr)

    with pytest.raises(RuntimeError, match="restart stderr thread start failed"):
        process.restart(time.monotonic() + 5)

    assert len(spawned) == 1
    assert spawned[0].process.poll() is not None
    if os.name == "nt":
        assert _windows_handle_status(int(spawned[0].process._handle)) == (False, 6)
    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    assert json.loads((process.owner_root / "failure.json").read_bytes())["code"] == (
        "restart_failed"
    )
    assert not (process.owner_root / "lease.json").exists()


def test_restart_spawn_failure_becomes_terminal_without_live_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    spawn_with_deadline = lsp_process.ProcessTree._spawn_with_deadline.__func__
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(
            lambda _cls, *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("restart spawn failed")
            )
        ),
    )

    with pytest.raises(OSError, match="restart spawn failed"):
        process.restart(time.monotonic() + 5)

    assert process.restart_count == 0
    assert process.state is ProcessState.FAILED
    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "restart_failed"
    assert not (process.owner_root / "lease.json").exists()
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(spawn_with_deadline),
    )
    _exercise_explicit_restart_candidate_cleanup_replaces_stopped_recovery_owner(
        monkeypatch, tmp_path
    )


def test_terminal_lease_removal_failure_does_not_skip_tree_or_protocol_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(
        tmp_path,
        "--lifecycle",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(tmp_path / "terminal-descendant.pid"),
    )
    owner = process._owner_directory
    assert owner is not None
    real_remove = lsp_process._OwnerDirectory.remove_lease

    def remove_lease(current: object) -> None:
        if current is owner:
            raise OSError("lease removal failed")
        real_remove(current)

    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "remove_lease",
        remove_lease,
    )

    with pytest.raises(OSError, match="lease removal failed"):
        process._terminal_failure("injected_failure", time.monotonic() + 5)

    assert process.process.poll() is not None
    assert process._tree is None
    assert not process.protocol.reader_thread.is_alive()
    assert not process.protocol.writer_thread.is_alive()
    assert process.state is ProcessState.FAILED
    monkeypatch.setattr(lsp_process._OwnerDirectory, "remove_lease", real_remove)
    process.close(time.monotonic() + 5)


def test_terminal_failure_retains_lease_until_protocol_owners_are_proven_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    lease = process.owner_root / "lease.json"
    close = process.protocol.close
    monkeypatch.setattr(
        process.protocol,
        "close",
        lambda _deadline: (_ for _ in ()).throw(TimeoutError("protocol owner blocked")),
    )

    with pytest.raises(TimeoutError, match="protocol owner blocked"):
        process._terminal_failure("injected_failure", time.monotonic() + 5)

    assert lease.is_file()
    monkeypatch.setattr(process.protocol, "close", close)
    process.protocol.close(time.monotonic() + 1)
    process.close(time.monotonic() + 5)


def test_heartbeat_stop_reports_deadline_with_live_owner_thread(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")

    with pytest.raises(TimeoutError, match="heartbeat"):
        process._stop_heartbeat(time.monotonic() - 1)

    process.close(time.monotonic() + 5)


def test_shutdown_retains_lease_and_scratch_until_protocol_owners_are_proven_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    owner = process.owner_root
    lease = owner / "lease.json"
    close = process.protocol.close
    monkeypatch.setattr(
        process.protocol,
        "close",
        lambda _deadline: (_ for _ in ()).throw(TimeoutError("protocol owner blocked")),
    )

    with pytest.raises(TimeoutError, match="protocol owner blocked"):
        process.shutdown(time.monotonic() + 5)

    assert owner.is_dir()
    assert lease.is_file()
    monkeypatch.setattr(process.protocol, "close", close)
    process.shutdown(time.monotonic() + 5)
    assert not owner.exists()


@pytest.mark.parametrize("failure_stage", ["owner-json", "protocol"])
def test_stubborn_child_preserves_restricted_failure_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str
) -> None:
    class StubbornProcess:
        def __init__(self) -> None:
            self.args = _command("--ignored-secret", "repository-secret")
            self.pid = 424242
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.terminate_calls = 0
            self.kill_calls = 0
            self.cleanup_allowed = False
            self.alive = True

        def poll(self) -> int | None:
            return None if self.alive else 1

        def wait(self, timeout: float | None = None) -> int:
            if not self.alive:
                return 1
            raise subprocess.TimeoutExpired(self.args, timeout)

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            if self.cleanup_allowed:
                self.alive = False

    class BrokenProtocol:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("protocol startup failed")

    child = StubbornProcess()
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(lambda _cls, *_args, **_kwargs: _FakeTree(child)),
    )
    if failure_stage == "owner-json":
        real_owner_write = lsp_process._write_owner_record
        writes = 0

        def fail_initial_owner_write(*args: object, **kwargs: object):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError("owner JSON failed")
            return real_owner_write(*args, **kwargs)

        monkeypatch.setattr(
            lsp_process,
            "_write_owner_record",
            fail_initial_owner_write,
        )
    else:
        monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(lsp_process.StartupCleanupError, match="retains retryable ownership") as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    expected_cause = OSError if failure_stage == "owner-json" else RuntimeError
    assert isinstance(raised.value.__cause__, expected_cause)
    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert owner.is_dir()
    owner_record = _optional_json(owner / "owner.json")
    failure_record = json.loads((owner / "failure.json").read_bytes())
    _assert_owner_state_for_stage(owner_record, failure_stage)
    assert failure_record["code"] == "startup_failed"
    assert failure_record["server_pid"] == child.pid
    assert failure_record["owner_nonce"] == OWNER_NONCE
    assert failure_record["timestamp"].endswith("Z")
    _assert_evidence_is_sanitized(owner, owner_record, tmp_path)
    _assert_failure_evidence_is_private(owner, owner_record)
    child.cleanup_allowed = True
    raised.value.retry_cleanup(time.monotonic() + 5)


def test_owner_permission_failure_rolls_back_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = False

    if os.name == "nt":
        def fail_permissions(_path: Path, _deadline: float) -> None:
            raise PermissionError("ACL unavailable")

        monkeypatch.setattr(lsp_process, "_secure_windows_owner_root", fail_permissions)
    else:
        def fail_permissions(_descriptor: int, _mode: int) -> None:
            raise PermissionError("mode unavailable")

        monkeypatch.setattr(lsp_process.os, "fchmod", fail_permissions)

    def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not run")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", unexpected_spawn)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(PermissionError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert spawned is False
    assert owner.is_dir()
    assert not any(owner.iterdir())


def test_immediate_parent_symlink_is_rejected_before_mutation(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    owner = linked_parent / OWNER_NONCE
    # POSIX refuses the linked parent by name; the Windows workspace refuses the
    # reparse point first. Both are the refusal this test exists for, and
    # neither may leave the owner directory behind.
    with pytest.raises((ValueError, PermissionError), match="parent|reparse point"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


def test_parent_handle_open_failure_occurs_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = False

    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "open",
        lambda _owner: (_ for _ in ()).throw(RuntimeError("parent handle failed")),
    )

    def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not run")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", unexpected_spawn)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="parent handle failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert spawned is False
    assert not owner.exists()


def test_parent_identity_change_after_spawn_terminates_child_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_verify = lsp_process._OwnerDirectory.verify_lexical_identity
    checks = 0

    def fail_first_fence(owner: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("parent identity changed during startup")
        real_verify(owner)

    monkeypatch.setattr(
        lsp_process._OwnerDirectory, "verify_lexical_identity", fail_first_fence
    )

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(RuntimeError, match="parent identity changed"):
        LspProcess.start(
            _command("--exit-while-pending"), cwd=tmp_path, owner_root=owner
        )
    assert len(children) == 1
    children[0].wait(timeout=5)
    assert children[0].poll() is not None
    assert owner.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse contract")
def test_windows_reparse_parent_is_rejected_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "reparse-parent"
    parent.mkdir()
    real_open = lsp_process._windows_workspace.open_directory_path

    def reject_reparse(path: Path) -> int:
        if path == parent:
            raise PermissionError("parent is a reparse point")
        return real_open(path)

    monkeypatch.setattr(
        lsp_process._windows_workspace, "open_directory_path", reject_reparse
    )
    owner = parent / OWNER_NONCE
    with pytest.raises(PermissionError, match="reparse"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows command contract")
@pytest.mark.parametrize("suffix", [".cmd", ".BAT"])
def test_windows_shell_scripts_are_rejected_before_mutation(
    tmp_path: Path, suffix: str
) -> None:
    executable = tmp_path / f"server{suffix}"
    executable.write_text("@echo off", encoding="utf-8")
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(ValueError, match="shell script"):
        LspProcess.start([str(executable)], cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable contract")
def test_posix_non_executable_file_is_rejected_before_mutation(tmp_path: Path) -> None:
    executable = tmp_path / "server"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o600)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(ValueError, match="executable"):
        LspProcess.start([str(executable)], cwd=tmp_path, owner_root=owner)
    assert not owner.exists()


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

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="protocol startup failed"):
        LspProcess.start(
            _command("--sleep-seconds", "30"),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
        )
    elapsed = time.monotonic() - started
    assert elapsed < lsp_process._STARTUP_WAIT_SECONDS + 0.75
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
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(lambda _cls, *_args, **_kwargs: _FakeTree(child)),
    )
    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(threading.Thread, "start", skip_stderr_owner)
    monkeypatch.setattr(
        lsp_process, "_secure_windows_owner_root", lambda _path, _deadline: None
    )
    owner = tmp_path / OWNER_NONCE
    started = time.monotonic()
    with pytest.raises(lsp_process.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    elapsed = time.monotonic() - started
    assert elapsed < lsp_process._STARTUP_WAIT_SECONDS + 0.75
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
        real_create = lsp_process._windows_workspace.create_file
        real_publish = lsp_process._windows_workspace.publish_file

        def race_publish(temporary_handle: int, parent: int, name: str) -> None:
            if name == "owner.json":
                attacker_handle = real_create(parent, name)
                try:
                    lsp_process._windows_workspace.write_all(
                        attacker_handle, attacker, chunk_bytes=4096
                    )
                    lsp_process._windows_workspace.flush_file(attacker_handle)
                finally:
                    lsp_process._windows_workspace.close_handle(attacker_handle)
            real_publish(temporary_handle, parent, name)

        monkeypatch.setattr(lsp_process._windows_workspace, "publish_file", race_publish)
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

        monkeypatch.setattr(lsp_process.os, "link", race_link)
    monkeypatch.setattr(
        lsp_process.subprocess,
        "Popen",
        lambda *args, **kwargs: children.append(real_popen(*args, **kwargs)) or children[-1],
    )
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(FileExistsError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert (owner / "owner.json").read_bytes() == attacker
    assert owner.is_dir()
    assert len(children) == 1
    children[0].wait(timeout=5)
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


def test_parent_swap_during_owner_write_rejects_and_preserves_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    real_write = lsp_process._write_owner_record
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

    monkeypatch.setattr(lsp_process, "_write_owner_record", write_then_swap)
    monkeypatch.setattr(
        lsp_process.subprocess,
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
    children[0].wait(timeout=5)
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
        if thread.name.startswith("lsp-exit-"):
            try:
                replacement = _replace_owner_root_with_attacker(owner, moved)
            except OSError:
                if os.name != "nt":
                    raise

    monkeypatch.setattr(threading.Thread, "start", start_then_swap)
    monkeypatch.setattr(
        lsp_process.subprocess,
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
    children[0].wait(timeout=5)
    assert children[0].poll() is not None


def test_replacement_before_startup_failure_write_is_never_mutated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = tmp_path / OWNER_NONCE
    moved = tmp_path / "moved-original"
    real_evidence = lsp_process._write_failure_record
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

    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process, "_write_failure_record", swap_before_evidence)
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
            children[0].wait(timeout=5)
        real_start(thread)

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
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
        real_create = lsp_process._windows_workspace.create_file

        def create_after_blocked_swap(parent: int, name: str) -> int:
            if name == "owner.json":
                with pytest.raises(OSError):
                    owner.rename(moved)
            return real_create(parent, name)

        monkeypatch.setattr(
            lsp_process._windows_workspace, "create_file", create_after_blocked_swap
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

        monkeypatch.setattr(lsp_process.os, "link", publish_after_swap)

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
    workspace = lsp_process._windows_workspace
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

    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
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
        lsp_process,
        "_secure_windows_owner_root",
        lambda _path, _deadline: None,
    )
    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "sync_directory",
        lambda _owner: None,
    )

    def start_untracked() -> LspProcess:
        return lsp_process._start_lsp_process(
            LspProcess,
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=tmp_path / OWNER_NONCE,
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
        lsp_process,
        "_secure_windows_owner_root",
        lambda _path, _deadline: None,
    )
    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
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
        process = lsp_process._start_lsp_process(
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
        )
        handles: list[int] = []
        exit_results: list[int] = []
        try:
            handles.append(open_identity(wait_for_pid_line(len(before))))
            assert process.request(
                "echo", {"cycle": index}, deadline=time.monotonic() + 10
            ) == {"cycle": index}
            handles.append(open_identity(wait_for_pid_line(len(before) + 1)))
            assert process.restart_count == 1
            assert process.state is not ProcessState.FAILED
            assert not (process.owner_root / "failure.json").exists()
        finally:
            try:
                process.close(time.monotonic() + 10)
            finally:
                for handle in handles:
                    exit_results.append(int(kernel32.WaitForSingleObject(handle, 2000)))
                    assert kernel32.CloseHandle(handle)

        created = pid_log.read_text(encoding="ascii").splitlines()[len(before) :]
        assert len(created) == 2
        assert exit_results == [0, 0]
        assert not process.owner_root.exists()
        assert not lsp_process._coordinator_has_ownership(process._coordinator)

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
        lsp_process._windows_workspace, "close_handle", lambda handle: closed.append(handle)
    )
    owner = lsp_process._OwnerDirectory(
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
    real_open = lsp_process._windows_workspace.open_deletable_directory
    fail_owner_open = True

    def open_deletable_directory(parent: int, name: str) -> int:
        if parent == owner_directory.parent_handle and name == process.owner_root.name:
            if fail_owner_open:
                raise OSError("owner delete open failed")
        return real_open(parent, name)

    monkeypatch.setattr(
        lsp_process._windows_workspace,
        "open_deletable_directory",
        open_deletable_directory,
    )

    with pytest.raises(OSError, match="owner delete open failed"):
        process.shutdown(time.monotonic() + 5)

    assert process.owner_root.is_dir()
    assert owner_directory.owner_handle is None
    assert owner_directory._closed is False
    assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING

    fail_owner_open = False
    process.shutdown(time.monotonic() + 5)
    assert not process.owner_root.exists()


def test_posix_scratch_parent_sync_failure_is_retryable_after_directory_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PosixOperations:
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
    owner = lsp_process._OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=object(),
        owner_handle=202,
        owner_identity=lsp_process._ObjectIdentity(1, 2, stat.S_IFDIR, 0),
    )
    monkeypatch.setattr(lsp_process, "os", operations)

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

    owner = lsp_process._OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=object(),
        owner_handle=202,
        owner_identity=lsp_process._ObjectIdentity(1, 2, stat.S_IFDIR, 0),
    )
    monkeypatch.setattr(lsp_process, "os", PosixOperations())

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

    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
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
    identity = compile_cache._windows_acl_identity()
    commands: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append((command, kwargs))
        output = f"{owner} {identity}:(OI)(CI)(F)\n".encode()
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("popen failed")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(lsp_process.subprocess, "Popen", fail_popen)

    with pytest.raises(OSError, match="popen failed"):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)

    broad = list(lsp_process._BROAD_ACL_SIDS)
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
        assert 0 < float(kwargs["timeout"]) <= lsp_process._STARTUP_WAIT_SECONDS
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

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fail_start, range(20)))

    assert max(elapsed for elapsed, _owner in results) <= 2.75
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
            (PermissionError, lsp_process.StartupCleanupError)
        ) as raised:
            LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
        elapsed = time.monotonic() - started
        if isinstance(raised.value, lsp_process.StartupCleanupError):
            raised.value.retry_cleanup(time.monotonic() + 5)
        return elapsed, owner

    monkeypatch.setattr(subprocess, "run", delayed_run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fail_start, range(4)))

    assert max(elapsed for elapsed, _owner in results) <= 2.75
    assert all(owner.is_dir() and not any(owner.iterdir()) for _elapsed, owner in results)
    for _elapsed, owner in results:
        owner.rmdir()
    assert _lsp_thread_names() == initial_threads


def _poll_until(predicate, timeout: float = 5.0) -> bool:
    """Wait for state the transition lock holder must not block on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _coordinator_wait(process: LspProcess, predicate: object, timeout: float = 3.0) -> bool:
    coordinator = process._coordinator
    with coordinator.condition:
        return coordinator.condition.wait_for(predicate, timeout=timeout)  # type: ignore[arg-type]


def test_fatal_intent_survives_transition_lock_contention_and_recovers_once(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    held = threading.Event()
    release = threading.Event()
    callback_returned = threading.Event()

    def hold_transition() -> None:
        with coordinator.condition:
            held.set()
            assert release.wait(2)

    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert held.wait(1)

    callback = threading.Thread(
        target=lambda: (
            process.protocol._become_fatal("injected fatal"),
            callback_returned.set(),
        )
    )
    callback.start()
    assert callback_returned.wait(0.5)
    release.set()
    holder.join(1)
    callback.join(1)

    assert _coordinator_wait(process, lambda: process.restart_count == 1)
    assert process.request("echo", {"ok": True}, deadline=time.monotonic() + 3) == {
        "ok": True
    }
    assert process.restart_count == 1
    process.close(time.monotonic() + 5)


def test_shutdown_waits_for_restart_candidate_ownership_to_be_attached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__
    spawned = threading.Event()
    release_spawn = threading.Event()
    shutdown_finished = threading.Event()
    candidate_trees: list[object] = []
    restart_errors: list[BaseException] = []
    shutdown_errors: list[BaseException] = []

    def blocked_spawn(cls: object, *args: object, **kwargs: object) -> object:
        tree = real_spawn(cls, *args, **kwargs)
        candidate_trees.append(tree)
        spawned.set()
        assert release_spawn.wait(3)
        return tree

    def restart() -> None:
        try:
            process.restart(time.monotonic() + 5)
        except BaseException as error:
            restart_errors.append(error)

    def shutdown() -> None:
        try:
            process.shutdown(time.monotonic() + 5)
        except BaseException as error:
            shutdown_errors.append(error)
        finally:
            shutdown_finished.set()

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(blocked_spawn),
    )
    restart_thread = threading.Thread(target=restart)
    shutdown_thread = threading.Thread(target=shutdown)
    restart_thread.start()
    assert spawned.wait(3)
    shutdown_thread.start()
    shutdown_returned_before_candidate_attachment = shutdown_finished.wait(0.2)
    release_spawn.set()
    restart_thread.join(5)
    shutdown_thread.join(5)

    assert shutdown_returned_before_candidate_attachment is False
    assert not restart_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert restart_errors == []
    assert shutdown_errors == []
    assert candidate_trees
    assert candidate_trees[0].process.poll() is not None
    assert not process.owner_root.exists()


def test_protocol_close_fault_retains_windows_tree_until_cleanup_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    tree = generation.tree
    assert tree is not None
    protocol = generation.protocol
    assert protocol is not None
    close = protocol.close
    monkeypatch.setattr(
        protocol,
        "close",
        lambda _deadline: (_ for _ in ()).throw(OSError("protocol close failed")),
    )

    try:
        with pytest.raises(OSError, match="protocol close failed"):
            process.shutdown(time.monotonic() + 5)

        if os.name == "nt":
            assert generation.tree is tree
        else:
            assert generation.tree is None
        assert generation.protocol is protocol
        assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert (process.owner_root / "lease.json").is_file()
    finally:
        monkeypatch.setattr(protocol, "close", close)
        process.shutdown(time.monotonic() + 5)

    assert generation.tree is None
    assert not process.owner_root.exists()


def test_cleanup_driver_aggregates_process_observation_error_and_continues() -> None:
    events: list[str] = []

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        @staticmethod
        def poll() -> int:
            events.append("poll")
            raise OSError("process poll failed")

    class Tree:
        @staticmethod
        def terminate(*, deadline: float) -> None:
            del deadline
            events.append("terminate")

        @staticmethod
        def close() -> None:
            events.append("tree-close")

    class Protocol:
        @staticmethod
        def _stop_io_for_process_cleanup() -> None:
            events.append("protocol-stop")

    generation = lsp_process._Generation(
        "b" * 32,
        Tree(),  # type: ignore[arg-type]
        Process(),  # type: ignore[arg-type]
        protocol=Protocol(),  # type: ignore[arg-type]
    )
    coordinator = lsp_process._LifecycleCoordinator(
        None,
        phase=lsp_process._LifecyclePhase.STOPPING_FAILURE,
        active=generation,
        terminal_outcome="failure",
        terminal_code="injected_failure",
    )

    errors = lsp_process._drive_cleanup(
        None,
        time.monotonic() + 1,
        terminal=True,
        failure_code="injected_failure",
        coordinator_override=coordinator,
    )

    assert any("process poll failed" in str(error) for error in errors)
    expected_events = ["protocol-stop", "terminate", "poll"]
    if os.name != "nt":
        expected_events.append("tree-close")
    assert events == expected_events
    if os.name == "nt":
        assert generation.tree is not None
    else:
        assert generation.tree is None
    assert generation.process is not None
    assert generation.protocol is not None
    assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING


def test_fatal_intent_after_cleanup_drain_dominates_success_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    server_pid = process.process.pid
    real_drain = lsp_process._drain_terminal_failures
    injected = False
    accepted: bool | None = None

    def drain_then_inject(
        instance: LspProcess | None,
        current: lsp_process._LifecycleCoordinator,
        deadline: float,
    ) -> None:
        nonlocal accepted, injected
        real_drain(instance, current, deadline)
        if current is coordinator and not injected:
            injected = True
            accepted = lsp_process._queue_owner_failure(
                current, "fatal at finalization barrier"
            )

    monkeypatch.setattr(lsp_process, "_drain_terminal_failures", drain_then_inject)

    process.close(time.monotonic() + 5)

    assert injected is True
    assert accepted is True
    failure_path = process.owner_root / "failure.json"
    failure = json.loads(failure_path.read_bytes())
    lsp_process._validate_failure_record(
        failure,
        code="heartbeat_failed",
        owner_nonce=process.owner_nonce,
        generation_nonce=process.generation_nonce,
        pid=server_pid,
    )
    assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
    assert coordinator.cleanup_result.evidence == "success"
    assert not (process.owner_root / "lease.json").exists()
    assert not lsp_process._coordinator_has_ownership(coordinator)


def test_cleanup_error_retention_is_bounded_sanitized_and_current_per_step() -> None:
    class PrivateRepositoryFailure(Exception):
        pass

    class ExplosiveDetailsFailure(OSError):
        @property
        def winerror(self) -> int:
            raise RuntimeError("D:/private/winerror")

        @property
        def errno(self) -> int:
            raise RuntimeError("D:/private/errno")

    class OversizedCodeFailure(OSError):
        errno = 1 << 128

    result = lsp_process._CleanupResult()

    for index in range(100):
        try:
            raise OSError(5, f"D:/private/repository-{index}")
        except OSError as error:
            result.failed("tree_termination", error)

    assert len(result.errors) == 1
    assert len(result.errors) <= lsp_process._MAX_RETAINED_CLEANUP_ERRORS
    retained = result.errors[0]
    assert retained.step == "tree_termination"
    assert retained.error_type == "OSError"
    assert retained.error_code == 5
    assert not hasattr(retained, "error")
    assert "private" not in repr(retained)
    assert not any(isinstance(value, BaseException) for value in dataclasses.astuple(retained))

    result.failed("tree_release", PrivateRepositoryFailure())

    assert result.errors[-1].error_type == "Exception"
    assert "PrivateRepository" not in repr(result.errors[-1])

    result.failed("protocol_stop", ExplosiveDetailsFailure())

    assert result.errors[-1] == lsp_process._CleanupError(
        step="protocol_stop", error_type="OSError", error_code=None
    )

    result.failed("evidence", OversizedCodeFailure())

    assert result.errors[-1] == lsp_process._CleanupError(
        step="evidence", error_type="OSError", error_code=None
    )

    result.succeeded("tree_termination")

    assert result.tree_termination == "success"
    assert [item.step for item in result.errors] == [
        "tree_release",
        "protocol_stop",
        "evidence",
    ]

    result.succeeded("tree_release")
    result.succeeded("protocol_stop")
    result.succeeded("evidence")

    assert result.errors == []


def test_background_cleanup_error_retention_drops_raw_exception_graph() -> None:
    coordinator = lsp_process._LifecycleCoordinator(None)
    error = OSError(5, "D:/private/background-cleanup")

    lsp_process._remember_background_cleanup_error(coordinator, error)

    retained = coordinator.background_cleanup_error
    assert retained is not None
    assert retained.error_type == "OSError"
    assert retained.error_code == 5
    assert not hasattr(retained, "error")
    assert "private" not in repr(retained)
    replayed = lsp_process._take_background_cleanup_error(coordinator)
    assert isinstance(replayed, RuntimeError)
    assert "private" not in str(replayed)
    assert coordinator.background_cleanup_error is None


def test_tree_cleanup_fault_retains_tree_owner_lease_and_scratch_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    assert generation.tree is not None
    tree = generation.tree
    heartbeat = coordinator.heartbeat_thread
    assert heartbeat is not None
    lease_path = process.owner_root / "lease.json"
    first_timestamp = json.loads(lease_path.read_bytes())["heartbeat_at"]
    terminate = lsp_process.ProcessTree.terminate
    fault_enabled = True

    def fail_tree(current: object, *, deadline: float) -> None:
        if current is tree and fault_enabled:
            raise OSError("tree terminate failed")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process.ProcessTree, "terminate", fail_tree)
    with pytest.raises((OSError, RuntimeError, TimeoutError), match="tree|live"):
        process.shutdown(time.monotonic() + 3)

    assert generation.tree is tree
    assert coordinator.owner_directory is not None
    assert (process.owner_root / "lease.json").is_file()
    assert (process.owner_root / "owner.json").is_file()
    assert coordinator.cleanup_result.ownership_pending is True
    assert heartbeat.is_alive()
    coordinator.heartbeat_wake.set()
    heartbeat_deadline = time.monotonic() + 2
    while time.monotonic() < heartbeat_deadline:
        if json.loads(lease_path.read_bytes())["heartbeat_at"] != first_timestamp:
            break
        time.sleep(0.01)
    assert json.loads(lease_path.read_bytes())["heartbeat_at"] != first_timestamp

    fault_enabled = False
    process.shutdown(time.monotonic() + 5)
    assert not heartbeat.is_alive()
    assert not process.owner_root.exists()
    assert coordinator.cleanup_result.errors == []


def test_heartbeat_failure_during_cleanup_pending_is_stale_and_observable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    assert generation.tree is not None
    tree = generation.tree
    heartbeat = coordinator.heartbeat_thread
    assert heartbeat is not None
    terminate = lsp_process.ProcessTree.terminate
    tree_fault = True

    def terminate_tree(current: object, *, deadline: float) -> None:
        if current is tree and tree_fault:
            raise OSError("tree remains live")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process.ProcessTree, "terminate", terminate_tree)
    with pytest.raises(OSError, match="tree remains live"):
        process.close(time.monotonic() + 3)

    lease_path = process.owner_root / "lease.json"
    stale_lease = lease_path.read_bytes()

    def fail_pending_heartbeat(_instance: LspProcess, _deadline: float) -> None:
        raise OSError("cleanup-pending heartbeat failed")

    monkeypatch.setattr(lsp_process, "_write_current_lease", fail_pending_heartbeat)
    coordinator.heartbeat_wake.set()
    heartbeat.join(1)
    assert not heartbeat.is_alive()
    assert lease_path.read_bytes() == stale_lease
    assert coordinator.background_cleanup_error is not None
    assert coordinator.background_cleanup_error.error_type == "OSError"
    assert process.state is ProcessState.DEGRADED

    tree_fault = False
    with pytest.raises(RuntimeError, match=r"background cleanup failed \(OSError\)"):
        process.close(time.monotonic() + 5)

    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "heartbeat_failed"
    assert not lease_path.exists()
    assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
    assert not lsp_process._coordinator_has_ownership(coordinator)


def test_live_child_defers_protocol_release_and_generation_joins_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--sleep-seconds", "30")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    tree = generation.tree
    protocol = generation.protocol
    assert tree is not None and protocol is not None
    terminate = lsp_process.ProcessTree.terminate
    stop_io = protocol._stop_io_for_process_cleanup
    close = protocol.close
    stop_calls: list[str] = []
    close_calls: list[str] = []
    tree_fault = True

    def stop_protocol_io() -> None:
        stop_calls.append("stop")
        stop_io()

    def unexpected_close(_deadline: float) -> None:
        close_calls.append("close")
        raise AssertionError("live child protocol ownership was released")

    def terminate_tree(current: object, *, deadline: float) -> None:
        if current is tree and tree_fault:
            raise OSError("tree still live")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "terminate",
        terminate_tree,
    )
    monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", stop_protocol_io)
    monkeypatch.setattr(protocol, "close", unexpected_close)

    with pytest.raises(OSError, match="tree still live"):
        process._terminal_failure("injected_failure", time.monotonic() + 0.2)

    assert process.process.poll() is None
    assert stop_calls == ["stop"]
    assert close_calls == []
    assert generation.protocol is protocol
    assert generation.exit_thread is not None
    assert coordinator.cleanup_result.ownership_pending is True
    assert (process.owner_root / "lease.json").is_file()

    tree_fault = False
    monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", stop_io)
    monkeypatch.setattr(protocol, "close", close)
    process.close(time.monotonic() + 5)
    assert process.process.poll() is not None
    assert not (process.owner_root / "lease.json").exists()


def test_periodic_heartbeat_write_failure_cannot_leave_running_without_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    attempted = threading.Event()
    real_write_lease = lsp_process._OwnerDirectory.write_lease

    def fail_heartbeat(current: object, record: object, **kwargs: object) -> None:
        if current is owner:
            attempted.set()
            raise OSError("heartbeat write failed")
        real_write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", fail_heartbeat)
    coordinator.heartbeat_wake.set()
    assert attempted.wait(1)
    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process._LifecyclePhase.RUNNING,
    )
    heartbeat = coordinator.heartbeat_thread
    assert not (
        coordinator.phase is lsp_process._LifecyclePhase.RUNNING
        and (heartbeat is None or not heartbeat.is_alive())
    )
    process.close(time.monotonic() + 5)
    assert process.state is ProcessState.FAILED
    assert json.loads((process.owner_root / "failure.json").read_bytes())["code"] == (
        "heartbeat_failed"
    )
    assert not (process.owner_root / "lease.json").exists()


def test_heartbeat_failure_leaves_running_before_blocked_recovery_can_continue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    heartbeat = coordinator.heartbeat_thread
    assert owner is not None and heartbeat is not None
    write_attempted = threading.Event()
    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    real_process_intent = lsp_process._process_failure_intent

    def fail_heartbeat(
        current: object, _record: object, **_kwargs: object
    ) -> None:
        if current is owner and threading.current_thread() is heartbeat:
            write_attempted.set()
            raise OSError("heartbeat write failed")
        raise AssertionError("unexpected lease writer")

    def block_recovery(
        current: LspProcess,
        intent: object,
        deadline: float,
    ) -> None:
        recovery_entered.set()
        assert release_recovery.wait(3)
        real_process_intent(current, intent, deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", fail_heartbeat)
    monkeypatch.setattr(lsp_process, "_process_failure_intent", block_recovery)
    coordinator.heartbeat_wake.set()
    try:
        assert write_attempted.wait(1)
        assert recovery_entered.wait(1)
        heartbeat.join(1)
        assert not heartbeat.is_alive()
        assert coordinator.phase is not lsp_process._LifecyclePhase.RUNNING
        assert process.state is ProcessState.DEGRADED
    finally:
        release_recovery.set()
        process.close(time.monotonic() + 5)


def test_heartbeat_failure_is_bounded_when_transition_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    heartbeat = coordinator.heartbeat_thread
    assert heartbeat is not None
    held = threading.Event()
    release = threading.Event()
    attempted = threading.Event()

    def hold_transition() -> None:
        with coordinator.condition:
            held.set()
            assert release.wait(30)

    def fail_heartbeat(_instance: LspProcess, _deadline: float) -> None:
        attempted.set()
        raise OSError("heartbeat write failed under contention")

    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 0.05)
    monkeypatch.setattr(lsp_process, "_write_current_lease", fail_heartbeat)
    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert held.wait(1)
    coordinator.heartbeat_wake.set()

    try:
        assert attempted.wait(5)
        heartbeat.join(5)
        assert not heartbeat.is_alive()
        assert coordinator.pending_failure_intents >= 1
        # The bounded TimeoutError is recorded by the recovery loop, and only
        # while this thread still holds the transition lock. Waiting for it
        # here keeps the later `close()` assertion from depending on how
        # quickly that thread is scheduled.
        assert _poll_until(lambda: coordinator.background_cleanup_error is not None)
    finally:
        release.set()
        holder.join(5)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process._LifecyclePhase.RUNNING,
    )
    with pytest.raises(RuntimeError, match=r"background cleanup failed \(TimeoutError\)"):
        process.close(time.monotonic() + 5)
    if lsp_process._coordinator_has_ownership(coordinator):
        process.close(time.monotonic() + 5)
    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "heartbeat_failed"


def test_expired_drain_inspection_is_bounded_when_transition_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    held = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    results: list[object] = []
    errors: list[BaseException] = []

    def hold_transition() -> None:
        with coordinator.condition:
            held.set()
            assert release.wait(3)

    def inspect() -> None:
        try:
            results.append(lsp_process._active_drain_generation(process))
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 0.05)
    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert held.wait(1)
    inspector = threading.Thread(target=inspect)
    inspector.start()
    try:
        assert completed.wait(0.25)
        assert results == [None]
        assert errors == []
        assert coordinator.background_cleanup_error is not None
        assert coordinator.background_cleanup_error.error_type == "TimeoutError"
    finally:
        release.set()
        holder.join(1)
        inspector.join(1)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process._LifecyclePhase.RUNNING,
    )
    with pytest.raises(RuntimeError, match=r"background cleanup failed \(TimeoutError\)"):
        process.close(time.monotonic() + 5)
    if lsp_process._coordinator_has_ownership(coordinator):
        process.close(time.monotonic() + 5)


def test_background_terminal_cleanup_error_is_observed_after_cleanup_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.protocol is not None
    protocol = generation.protocol
    stop_io = protocol._stop_io_for_process_cleanup
    failed = threading.Event()

    def fail_background_stop() -> None:
        if threading.current_thread() is coordinator.recovery_thread:
            failed.set()
            raise OSError("background protocol cleanup failed")
        stop_io()

    monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", fail_background_stop)
    lsp_process._queue_owner_failure(coordinator, "injected owner failure")

    assert failed.wait(2)
    recovery = coordinator.recovery_thread
    assert recovery is not None
    recovery.join(2)
    assert not recovery.is_alive()
    assert coordinator.cleanup_result.ownership_pending is False
    monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", stop_io)

    with pytest.raises(RuntimeError, match=r"background cleanup failed \(OSError\)"):
        process.close(time.monotonic() + 5)

    assert not lsp_process._coordinator_has_ownership(coordinator)
    process.close(time.monotonic() + 1)


def test_restart_waits_for_inflight_heartbeat_before_publishing_new_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    first_generation = process.generation_nonce
    heartbeat_writing = threading.Event()
    release_heartbeat = threading.Event()
    restart_finished = threading.Event()
    restart_acquiring_lease = threading.Event()
    restart_errors: list[BaseException] = []
    real_write_lease = lsp_process._OwnerDirectory.write_lease
    real_acquire_lease = lsp_process._acquire_lease
    restart_thread: threading.Thread | None = None

    def delayed_heartbeat(current: object, record: object, **kwargs: object) -> None:
        assert isinstance(record, dict)
        if (
            current is owner
            and threading.current_thread() is coordinator.heartbeat_thread
            and record["generation_nonce"] == first_generation
        ):
            heartbeat_writing.set()
            assert release_heartbeat.wait(3)
        real_write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    def restart() -> None:
        try:
            process.restart(time.monotonic() + 5)
        except BaseException as error:
            restart_errors.append(error)
        finally:
            restart_finished.set()

    def observe_lease_acquisition(
        current: object,
        deadline: float,
        *,
        allow_expired: bool = False,
    ) -> None:
        if threading.current_thread() is restart_thread:
            restart_acquiring_lease.set()
        real_acquire_lease(
            current,  # type: ignore[arg-type]
            deadline,
            allow_expired=allow_expired,
        )

    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "write_lease",
        delayed_heartbeat,
    )
    monkeypatch.setattr(lsp_process, "_acquire_lease", observe_lease_acquisition)
    coordinator.heartbeat_wake.set()
    assert heartbeat_writing.wait(2)
    restart_thread = threading.Thread(target=restart)
    restart_thread.start()
    assert restart_acquiring_lease.wait(2)
    assert not restart_finished.is_set()
    release_heartbeat.set()
    restart_thread.join(5)

    assert not restart_thread.is_alive()
    assert restart_errors == []
    assert process.generation_nonce != first_generation
    lease = json.loads((process.owner_root / "lease.json").read_bytes())
    assert lease["generation_nonce"] == process.generation_nonce
    assert lease["server_pid"] == process.process.pid
    process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX atomic lease publication")
def test_posix_lease_publish_failure_preserves_previous_bytes_and_no_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process._OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    owner.write_lease({"version": "old"})
    before = (owner_root / "lease.json").read_bytes()
    monkeypatch.setattr(
        lsp_process.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        owner.write_lease({"version": "new"})

    assert (owner_root / "lease.json").read_bytes() == before
    assert not list(owner_root.glob(".lease-*.tmp"))
    owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows lease sharing semantics")
def test_windows_lease_refresh_waits_for_a_temporary_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    lease_path = process.owner_root / "lease.json"
    before = lease_path.read_bytes()
    replace_file = lsp_process._windows_workspace.replace_file
    sharing_blocked = threading.Event()
    attempts = 0
    errors: list[BaseException] = []

    def observe_replace(handle: int, parent: int, name: str) -> None:
        nonlocal attempts
        attempts += 1
        try:
            replace_file(handle, parent, name)
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            if code in {5, 32, 33}:
                sharing_blocked.set()
            raise

    def refresh() -> None:
        try:
            lsp_process._write_current_lease(process, time.monotonic() + 2)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(
        lsp_process._windows_workspace,
        "replace_file",
        observe_replace,
    )
    reader = lease_path.open("rb")
    worker = threading.Thread(target=refresh)
    try:
        worker.start()
        assert sharing_blocked.wait(1)
        assert worker.is_alive()
        reader.close()
        worker.join(3)
        assert not worker.is_alive()
        assert errors == []
        assert attempts >= 2
        assert lease_path.read_bytes() != before
        assert not list(process.owner_root.glob(".lease-*.tmp"))
    finally:
        reader.close()
        worker.join(3)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "nt", reason="Windows lease sharing semantics")
@pytest.mark.parametrize(
    ("error_code", "expected_attempts"),
    [(5, 2), (32, 2), (33, 2), (87, 1)],
)
def test_windows_lease_retries_only_transient_sharing_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_code: int,
    expected_attempts: int,
) -> None:
    owner = _mock_windows_artifact_owner(
        monkeypatch,
        tmp_path,
        lambda _handle: None,
    )
    attempts = 0

    def replace_file(_handle: int, _parent: int, _name: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(error_code, "injected lease replacement failure")

    monkeypatch.setattr(lsp_process._windows_workspace, "replace_file", replace_file)
    now = time.monotonic()
    if error_code == 87:
        with pytest.raises(OSError) as raised:
            owner.write_lease(
                {"state": "live"},
                deadline=now + 1,
                expires_monotonic=now + 30,
                retry_stop=threading.Event(),
            )
        assert raised.value.errno == error_code
    else:
        owner.write_lease(
            {"state": "live"},
            deadline=now + 1,
            expires_monotonic=now + 30,
            retry_stop=threading.Event(),
        )

    assert attempts == expected_attempts


@pytest.mark.skipif(os.name != "nt", reason="Windows lease sharing semantics")
def test_windows_lease_retry_cannot_outlive_the_published_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _mock_windows_artifact_owner(
        monkeypatch,
        tmp_path,
        lambda _handle: None,
    )
    now = time.monotonic()
    previous_expiry = now + 0.08
    owner.write_lease(
        {"generation": "old"},
        deadline=now + 1,
        expires_monotonic=previous_expiry,
        retry_stop=threading.Event(),
    )
    attempts = 0

    def sharing_violation(_handle: int, _parent: int, _name: str) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(32, "lease reader is still open")

    monkeypatch.setattr(
        lsp_process._windows_workspace,
        "replace_file",
        sharing_violation,
    )
    started = time.monotonic()

    with pytest.raises(OSError) as raised:
        owner.write_lease(
            {"generation": "new"},
            deadline=started + 1,
            expires_monotonic=started + 30,
            retry_stop=threading.Event(),
        )

    assert raised.value.errno == 32
    assert attempts >= 2
    assert time.monotonic() <= previous_expiry + 0.15


def test_successful_failure_evidence_is_published_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    write_failure = lsp_process._write_failure_record
    publications = 0

    def count_publication(*args: object, **kwargs: object) -> None:
        nonlocal publications
        publications += 1
        write_failure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process, "_write_failure_record", count_publication)
    try:
        process._terminal_failure("injected_failure", time.monotonic() + 5)
    finally:
        monkeypatch.setattr(lsp_process, "_write_failure_record", write_failure)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)

    assert publications == 1
    assert coordinator.cleanup_result.evidence == "success"


def test_failed_state_and_waiter_notification_follow_durable_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    heartbeat = coordinator.heartbeat_thread
    assert owner is not None and heartbeat is not None
    write_failure = lsp_process._write_failure_record
    write_lease = lsp_process._OwnerDirectory.write_lease
    evidence_started = threading.Event()
    release_evidence = threading.Event()
    heartbeat_wrote = threading.Event()
    waiter_ready = threading.Event()
    waiter_finished = threading.Event()
    waiter_results: list[bool] = []
    cleanup_errors: list[BaseException] = []

    def block_evidence(*args: object, **kwargs: object) -> None:
        evidence_started.set()
        assert release_evidence.wait(3)
        write_failure(*args, **kwargs)  # type: ignore[arg-type]

    def observe_heartbeat(current: object, record: object, **kwargs: object) -> None:
        if (
            current is owner
            and threading.current_thread() is heartbeat
            and evidence_started.is_set()
            and not release_evidence.is_set()
        ):
            heartbeat_wrote.set()
        write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    def wait_for_failed() -> None:
        with coordinator.condition:
            waiter_ready.set()
            waiter_results.append(
                coordinator.condition.wait_for(
                    lambda: process.state is ProcessState.FAILED,
                    timeout=3,
                )
            )
        waiter_finished.set()

    def fail_terminally() -> None:
        try:
            process._terminal_failure("injected_failure", time.monotonic() + 5)
        except BaseException as error:
            cleanup_errors.append(error)

    monkeypatch.setattr(lsp_process, "_write_failure_record", block_evidence)
    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", observe_heartbeat)
    waiter = threading.Thread(target=wait_for_failed)
    cleanup = threading.Thread(target=fail_terminally)
    waiter.start()
    assert waiter_ready.wait(1)
    cleanup.start()
    try:
        assert evidence_started.wait(2), cleanup_errors
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPING_FAILURE
        assert process.state is ProcessState.DEGRADED
        assert waiter_finished.wait(0.2) is False
        assert not (process.owner_root / "failure.json").exists()
        assert (process.owner_root / "lease.json").is_file()
        coordinator.heartbeat_wake.set()
        assert heartbeat_wrote.wait(1)
    finally:
        release_evidence.set()
        cleanup.join(5)
        waiter.join(5)
        monkeypatch.setattr(lsp_process, "_write_failure_record", write_failure)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)

    assert not cleanup.is_alive()
    assert not waiter.is_alive()
    assert cleanup_errors == []
    assert waiter_results == [True]
    assert process.state is ProcessState.FAILED
    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    lsp_process._validate_failure_record(
        failure,
        code="injected_failure",
        owner_nonce=process.owner_nonce,
        generation_nonce=process.generation_nonce,
        pid=process.process.pid,
    )


def test_failure_evidence_write_fault_is_observable_and_tree_cleanup_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    write_record = lsp_process._OwnerDirectory.write_record
    fault_enabled = True

    def fail_failure(current: object, name: str, record: object) -> None:
        if current is owner and name == "failure.json" and fault_enabled:
            raise OSError("failure evidence flush failed")
        write_record(current, name, record)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_record", fail_failure)
    with pytest.raises(OSError, match="failure evidence flush failed"):
        process._terminal_failure("injected_failure", time.monotonic() + 2)

    failure = process.owner_root / "failure.json"
    if failure.exists():
        json.loads(failure.read_bytes())
    generation = coordinator.active
    assert generation is None or generation.tree is None or generation.process.poll() is not None
    assert coordinator.cleanup_result.evidence == "failed"
    assert coordinator.cleanup_result.ownership_pending is True
    assert process.state is ProcessState.DEGRADED
    assert (process.owner_root / "lease.json").is_file()

    fault_enabled = False
    process.close(time.monotonic() + 5)
    assert process.state is ProcessState.FAILED
    assert json.loads(failure.read_bytes())["code"] == "injected_failure"
    assert not (process.owner_root / "lease.json").exists()


def test_evidence_payload_failure_leaves_no_partial_canonical_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process._OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)

    if os.name == "nt":
        real_windows_write = lsp_process._windows_workspace.write_all

        def partial_windows_write(handle: int, payload: bytes, *, chunk_bytes: int) -> None:
            real_windows_write(handle, payload[:5], chunk_bytes=chunk_bytes)
            raise OSError("evidence write failed")

        monkeypatch.setattr(
            lsp_process._windows_workspace,
            "write_all",
            partial_windows_write,
        )
    else:
        real_write = lsp_process._write_all_descriptor

        def partial_write(descriptor: int, payload: bytes) -> None:
            os.write(descriptor, payload[:5])
            raise OSError("evidence write failed")

        monkeypatch.setattr(lsp_process, "_write_all_descriptor", partial_write)

    with pytest.raises(OSError, match="evidence (flush|write) failed"):
        owner.write_record("failure.json", {"code": "injected"})

    canonical = owner_root / "failure.json"
    assert not canonical.exists() or json.loads(canonical.read_bytes()) == {
        "code": "injected"
    }
    assert not list(owner_root.glob(".evidence-*.tmp"))
    if os.name != "nt":
        monkeypatch.setattr(lsp_process, "_write_all_descriptor", real_write)
    owner.close()


def test_existing_canonical_but_invalid_failure_evidence_is_never_accepted(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    failure = process.owner_root / "failure.json"
    failure.write_bytes(
        json.dumps(
            {
                "code": None,
                "generation_nonce": "invalid",
                "owner_nonce": process.owner_nonce,
                "timestamp": "not-a-timestamp",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    try:
        with pytest.raises(ValueError, match="failure evidence"):
            process._terminal_failure("injected_failure", time.monotonic() + 5)

        assert process.process.poll() is not None
        assert process._coordinator.cleanup_result.evidence == "failed"
        assert process._coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert (process.owner_root / "lease.json").is_file()
    finally:
        if failure.exists():
            failure.unlink()
        if lsp_process._coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-owner",
        "wrong-generation",
        "wrong-code",
        "missing-pid",
        "wrong-pid",
        "noncanonical-timestamp",
    ],
)
def test_existing_failure_evidence_must_match_terminal_identity_and_stays_sticky(
    tmp_path: Path,
    mutation: str,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    record: dict[str, object] = {
        "code": "heartbeat_failed",
        "generation_nonce": process.generation_nonce,
        "owner_nonce": process.owner_nonce,
        "server_pid": process.process.pid,
        "timestamp": "2026-07-24T12:34:56.123456Z",
    }
    _mutate_failure_record(record, mutation, process.process.pid)
    failure = process.owner_root / "failure.json"
    failure.write_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    try:
        lsp_process._queue_owner_failure(coordinator, "injected owner failure")
        recovery = coordinator.recovery_thread
        assert recovery is not None
        recovery.join(5)

        assert not recovery.is_alive()
        assert coordinator.failure_evidence_identity is not None
        assert coordinator.failure_evidence_identity.code == "heartbeat_failed"
        assert coordinator.background_cleanup_error is not None
        assert coordinator.background_cleanup_error.error_type == "ValueError"
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is not None
        assert (process.owner_root / "lease.json").is_file()

        failure.unlink()
        with pytest.raises(RuntimeError, match=r"background cleanup failed \(ValueError\)"):
            process.close(time.monotonic() + 5)
        assert not lsp_process._coordinator_has_ownership(coordinator)
        process.close(time.monotonic() + 1)
    finally:
        if failure.exists():
            failure.unlink()
        if lsp_process._coordinator_has_ownership(coordinator):
            try:
                process.close(time.monotonic() + 5)
            except ValueError:
                process.close(time.monotonic() + 5)


@pytest.mark.parametrize("artifact", ["evidence", "lease"])
def test_posix_partial_artifact_write_removes_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    class FileInfo:
        st_dev = 1
        st_ino = 2
        st_mode = stat.S_IFREG | 0o600

    class PosixOperations:
        name = "posix"
        O_WRONLY = os.O_WRONLY
        O_CREAT = os.O_CREAT
        O_EXCL = os.O_EXCL
        O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
        O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

        def __init__(self) -> None:
            self.created: list[str] = []
            self.closed: list[int] = []
            self.unlinked: list[str] = []

        def open(
            self,
            path: str,
            _flags: int,
            _mode: int,
            *,
            dir_fd: int,
        ) -> int:
            assert dir_fd == 202
            self.created.append(path)
            return 303

        @staticmethod
        def fstat(descriptor: int) -> FileInfo:
            assert descriptor == 303
            return FileInfo()

        def close(self, descriptor: int) -> None:
            self.closed.append(descriptor)

        def unlink(self, path: str, *, dir_fd: int) -> None:
            assert dir_fd == 202
            self.unlinked.append(path)

    operations = PosixOperations()
    owner = lsp_process._OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=object(),
        owner_handle=202,
        owner_identity=object(),
        owner_permissions_verified=True,
    )
    monkeypatch.setattr(lsp_process, "os", operations)
    monkeypatch.setattr(
        lsp_process,
        "_write_all_descriptor",
        lambda _descriptor, _payload: (_ for _ in ()).throw(
            OSError("partial artifact write")
        ),
    )

    with pytest.raises(OSError, match="partial artifact write"):
        if artifact == "evidence":
            owner.write_record("failure.json", {"code": "injected"})
        else:
            owner.write_lease({"state": "live"})

    assert len(operations.created) == 1
    assert operations.closed == [303]
    assert operations.unlinked == operations.created


def _block_native_temporary_deletion(
    monkeypatch: pytest.MonkeyPatch,
    owner_root: Path,
    prefix: str,
    message: str,
) -> tuple[threading.Event, list[str]]:
    allowed = threading.Event()
    attempts: list[str] = []
    if os.name == "nt":
        workspace = lsp_process._windows_workspace
        real_create = workspace.create_file
        real_open = workspace.open_deletable_file
        real_delete = workspace.delete_handle
        temporary_handles: set[int] = set()

        def create_file(parent: int, name: str) -> int:
            handle = real_create(parent, name)
            if name.startswith(prefix) and name.endswith(".tmp"):
                temporary_handles.add(handle)
            return handle

        def open_deletable_file(parent: int, name: str) -> int:
            handle = real_open(parent, name)
            if name.startswith(prefix) and name.endswith(".tmp"):
                temporary_handles.add(handle)
            return handle

        def delete_handle(handle: int) -> None:
            if handle in temporary_handles:
                attempts.extend(
                    sorted(path.name for path in owner_root.glob(f"{prefix}*.tmp"))
                )
                _refuse_until_allowed(allowed, message)
            real_delete(handle)
            temporary_handles.discard(handle)

        monkeypatch.setattr(workspace, "create_file", create_file)
        monkeypatch.setattr(workspace, "open_deletable_file", open_deletable_file)
        monkeypatch.setattr(workspace, "delete_handle", delete_handle)
    else:
        real_unlink = lsp_process.os.unlink

        def unlink(name: str, *, dir_fd: int | None = None) -> None:
            if _is_blocked_temporary(name, prefix):
                attempts.append(name)
                _refuse_until_allowed(allowed, message)
            if dir_fd is None:
                real_unlink(name)
                return
            real_unlink(name, dir_fd=dir_fd)

        monkeypatch.setattr(lsp_process.os, "unlink", unlink)
    return allowed, attempts


def test_failure_temp_cleanup_blocks_canonical_evidence_finalization_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    deletion_allowed, deletion_attempts = _block_native_temporary_deletion(
        monkeypatch,
        process.owner_root,
        ".evidence-",
        "evidence temporary deletion failed",
    )

    try:
        if os.name == "nt":
            writer_owner = lsp_process._windows_workspace
            writer_name = "write_all"
        else:
            writer_owner = lsp_process
            writer_name = "_write_all_descriptor"
        real_write = getattr(writer_owner, writer_name)
        monkeypatch.setattr(
            writer_owner,
            writer_name,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("evidence temporary write failed")
            ),
        )
        with pytest.raises(OSError, match="evidence temporary deletion failed"):
            owner.write_record("failure.json", {"code": "one-shot"})
        assert len(owner._pending_temp_names) == 1
        assert len(list(process.owner_root.glob(".evidence-*.tmp"))) == 1

        deletion_allowed.set()
        owner._retry_pending_temp_names()
        monkeypatch.setattr(writer_owner, writer_name, real_write)
        assert owner._pending_temp_names == []
        assert not list(process.owner_root.glob(".evidence-*.tmp"))

        lsp_process._write_failure_record(
            owner,
            code="injected_failure",
            owner_nonce=process.owner_nonce,
            generation_nonce=process.generation_nonce,
            pid=process.process.pid,
        )
        deletion_allowed.clear()
        with pytest.raises(OSError, match="evidence temporary deletion failed"):
            process._terminal_failure("injected_failure", time.monotonic() + 5)

        assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is owner
        assert owner._closed is False
        assert process.state is ProcessState.DEGRADED
        assert (process.owner_root / "lease.json").is_file()
        pending = list(owner._pending_temp_names)
        assert len(pending) == 1
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "lease.json",
            "failure.json",
            pending[0],
        }
        first_attempt_count = len(deletion_attempts)

        with pytest.raises(OSError, match="evidence temporary deletion failed"):
            process.close(time.monotonic() + 5)

        assert len(deletion_attempts) > first_attempt_count
        assert owner._pending_temp_names == pending
        assert list(process.owner_root.glob(".evidence-*.tmp")) == [
            process.owner_root / pending[0]
        ]
        assert ("evidence", "OSError") in _cleanup_error_steps(coordinator)

        deletion_allowed.set()
        process.close(time.monotonic() + 5)

        assert owner._pending_temp_names == []
        assert owner._closed is True
        assert coordinator.owner_directory is None
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
        assert process.state is ProcessState.FAILED
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "failure.json",
        }
    finally:
        deletion_allowed.set()
        _remove_temporaries(process.owner_root, ".evidence-*.tmp")
        _close_if_owned(process, coordinator)


def test_lease_temp_cleanup_blocks_success_finalization_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None
    lease = process.owner_root / "lease.json"
    lease_before = lease.read_bytes()
    deletion_allowed, deletion_attempts = _block_native_temporary_deletion(
        monkeypatch,
        process.owner_root,
        ".lease-",
        "lease temporary deletion failed",
    )
    if os.name == "nt":
        writer_owner = lsp_process._windows_workspace
        writer_name = "write_all"
    else:
        writer_owner = lsp_process
        writer_name = "_write_all_descriptor"
    real_write = getattr(writer_owner, writer_name)
    monkeypatch.setattr(
        writer_owner,
        writer_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("lease temporary write failed")
        ),
    )

    try:
        for _attempt in range(2):
            _expect_blocked_lease_write(coordinator, owner)
        monkeypatch.setattr(writer_owner, writer_name, real_write)

        pending = list(owner._pending_temp_names)
        assert len(pending) == 1
        assert len(set(deletion_attempts)) == 1
        assert lease.read_bytes() == lease_before
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "lease.json",
            pending[0],
        }

        with pytest.raises(OSError, match="lease temporary deletion failed"):
            process.shutdown(time.monotonic() + 5)

        assert coordinator.terminal_outcome == "success"
        assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert coordinator.cleanup_result.lease_removal == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is owner
        assert owner._closed is False
        assert lease.read_bytes() == lease_before
        assert owner._pending_temp_names == pending
        assert _owner_entry_names(process) == {
            "cancellation",
            "owner.json",
            "lease.json",
            pending[0],
        }

        with pytest.raises(OSError, match="lease temporary deletion failed"):
            process.shutdown(time.monotonic() + 5)
        assert owner._pending_temp_names == pending
        assert list(process.owner_root.glob(".lease-*.tmp")) == [
            process.owner_root / pending[0]
        ]
        assert ("lease_removal", "OSError") in _cleanup_error_steps(coordinator)

        deletion_allowed.set()
        process.shutdown(time.monotonic() + 5)

        assert owner._pending_temp_names == []
        assert owner._closed is True
        assert coordinator.owner_directory is None
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_SUCCESS
        assert not process.owner_root.exists()
    finally:
        monkeypatch.setattr(writer_owner, writer_name, real_write)
        deletion_allowed.set()
        _remove_temporaries(process.owner_root, ".lease-*.tmp")
        _close_if_owned(process, coordinator)


def test_startup_rollback_kills_descendant_after_direct_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "startup-descendant.pid"
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_after_leader_exit(*_args: object, **_kwargs: object) -> None:
        assert children
        children[0].wait(timeout=2)
        raise RuntimeError("startup publication failed")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen)
    monkeypatch.setattr(lsp_process, "_write_owner_record", fail_after_leader_exit)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises((RuntimeError, lsp_process.StartupCleanupError)) as raised:
        LspProcess.start(
            _command(
                "--spawn-descendant",
                "--descendant-pid-file",
                str(pid_file),
                "--exit-after-descendant-spawn",
            ),
            cwd=tmp_path,
            owner_root=owner,
        )

    descendant = int(pid_file.read_text(encoding="ascii"))
    settle_deadline = time.monotonic() + 2
    while _pid_alive(descendant) and time.monotonic() < settle_deadline:
        time.sleep(0.02)
    if _pid_alive(descendant):
        assert isinstance(raised.value, lsp_process.StartupCleanupError)
        assert raised.value.coordinator.cleanup_result.ownership_pending is True
    else:
        assert json.loads((owner / "failure.json").read_bytes())["code"] == "startup_failed"
    if isinstance(raised.value, lsp_process.StartupCleanupError):
        raised.value.retry_cleanup(time.monotonic() + 5)


def test_blocked_protocol_owner_retains_retryable_generation_and_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.protocol is not None
    protocol = generation.protocol
    join_owners = protocol._join_owners
    released = threading.Event()

    def blocked(deadline: float) -> None:
        if not released.is_set():
            raise TimeoutError("protocol owner blocked")
        join_owners(deadline)

    monkeypatch.setattr(protocol, "_join_owners", blocked)
    with pytest.raises(TimeoutError, match="protocol owner blocked"):
        process.shutdown(time.monotonic() + 5)

    assert generation.protocol is protocol
    assert coordinator.cleanup_result.ownership_pending is True
    assert (process.owner_root / "lease.json").is_file()

    released.set()
    process.shutdown(time.monotonic() + 5)
    assert not process.owner_root.exists()


def _cyclic_value() -> list[object]:
    value: list[object] = []
    value.append(value)
    return value


def _invalid_request_params(case: str) -> object:
    """One payload per way a caller can violate the JSON wire contract."""
    cases = {
        "cycle": _cyclic_value,
        "nan": lambda: {"value": float("nan")},
        "inf": lambda: {"value": float("inf")},
        "surrogate": lambda: {"value": "\ud800"},
        "object": lambda: {"value": object()},
        "oversize": lambda: {"value": "x" * MAX_FRAME_BYTES},
    }
    return cases[case]()


@pytest.mark.parametrize("case", ["cycle", "nan", "inf", "surrogate", "object", "oversize"])
def test_caller_json_violation_never_restarts_and_valid_follow_up_works(
    tmp_path: Path,
    case: str,
) -> None:
    owner = tmp_path / f"{len(case):032x}"
    process = LspProcess.start(_command("--lifecycle"), cwd=tmp_path, owner_root=owner)

    with pytest.raises(ProtocolViolation):
        process.request(
            "invalid",
            _invalid_request_params(case),
            deadline=time.monotonic() + 2,
        )

    assert process.restart_count == 0
    assert process.request("echo", {"valid": True}, deadline=time.monotonic() + 2) == {
        "valid": True
    }
    process.close(time.monotonic() + 5)


def test_caller_json_violation_is_not_retried_after_concurrent_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    first_generation = process.generation_nonce
    real_encode = lsp_protocol.encode_frame
    encoding = threading.Event()
    release_encoding = threading.Event()
    attempts = 0
    request_errors: list[BaseException] = []

    def block_invalid_encoding(message: object) -> bytes:
        nonlocal attempts
        if isinstance(message, dict) and message.get("method") == "invalid":
            attempts += 1
            if attempts == 1:
                encoding.set()
                assert release_encoding.wait(3)
        return real_encode(message)

    def request_invalid() -> None:
        try:
            process.request(
                "invalid",
                _invalid_request_params("cycle"),
                deadline=time.monotonic() + 5,
            )
        except BaseException as error:
            request_errors.append(error)

    monkeypatch.setattr(lsp_protocol, "encode_frame", block_invalid_encoding)
    request_thread = threading.Thread(target=request_invalid)
    request_thread.start()
    assert encoding.wait(2)
    process.restart(time.monotonic() + 5)
    assert process.generation_nonce != first_generation
    release_encoding.set()
    request_thread.join(3)

    assert not request_thread.is_alive()
    assert len(request_errors) == 1
    assert isinstance(request_errors[0], ProtocolViolation)
    assert attempts == 1
    assert process.restart_count == 1
    assert process.request("echo", {"valid": True}, deadline=time.monotonic() + 2) == {
        "valid": True
    }
    process.close(time.monotonic() + 5)


def test_expired_deadline_waiting_for_transition_lock_changes_nothing(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    acquired = threading.Event()
    release = threading.Event()
    before = (
        process.process,
        process.protocol,
        process.generation_nonce,
        process.restart_count,
        process.state,
        coordinator.phase,
        coordinator.recovery_attempted,
        generation.expected_exit.is_set(),
        coordinator.recovery_wake.is_set(),
    )

    def hold_transition() -> None:
        with coordinator.condition:
            acquired.set()
            assert release.wait(2)

    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert acquired.wait(1)
    deadline = time.monotonic() + 0.03
    try:
        with pytest.raises(TimeoutError, match="lifecycle|transition"):
            process.restart(deadline)
        assert time.monotonic() <= deadline + 0.2
        assert (
            process.process,
            process.protocol,
            process.generation_nonce,
            process.restart_count,
            process.state,
            coordinator.phase,
            coordinator.recovery_attempted,
            generation.expected_exit.is_set(),
            coordinator.recovery_wake.is_set(),
        ) == before
        assert coordinator.candidate is None
    finally:
        release.set()
        holder.join(1)
        process.close(time.monotonic() + 5)


def test_explicit_restart_timeout_after_pending_hands_off_one_recovery_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    assert recovery is not None
    first_generation = process.generation_nonce
    seize = threading.Event()
    held = threading.Event()
    release = threading.Event()
    restart_thread = threading.current_thread()
    release_lifecycle = lsp_process._release_lifecycle
    prepare_generation = lsp_process._prepare_generation
    prepared_by: list[threading.Thread] = []
    armed = True

    def hold_transition() -> None:
        assert seize.wait(2)
        with coordinator.condition:
            held.set()
            assert release.wait(5)

    def release_then_contend(current: lsp_process._LifecycleCoordinator) -> None:
        nonlocal armed
        pending = (
            current is coordinator
            and armed
            and threading.current_thread() is restart_thread
            and current.phase is lsp_process._LifecyclePhase.RECOVERY_PENDING
        )
        release_lifecycle(current)
        if pending:
            armed = False
            seize.set()
            assert held.wait(1)

    def observe_prepare(*args: object, **kwargs: object) -> lsp_process._Generation:
        prepared_by.append(threading.current_thread())
        return prepare_generation(*args, **kwargs)  # type: ignore[arg-type]

    holder = threading.Thread(target=hold_transition)
    holder.start()
    monkeypatch.setattr(lsp_process, "_release_lifecycle", release_then_contend)
    monkeypatch.setattr(lsp_process, "_prepare_generation", observe_prepare)

    try:
        with pytest.raises(TimeoutError, match="lifecycle transition lock"):
            process.restart(time.monotonic() + 0.1)

        assert coordinator.phase is lsp_process._LifecyclePhase.RECOVERY_PENDING
        assert process.restart_count == 0
        assert prepared_by == []

        release.set()
        holder.join(1)

        assert _coordinator_wait(process, lambda: process.restart_count == 1, timeout=5)
        assert coordinator.phase is lsp_process._LifecyclePhase.RUNNING
        assert process.generation_nonce != first_generation
        assert prepared_by == [recovery]
        assert process.request("echo", {"ok": True}, deadline=time.monotonic() + 2) == {
            "ok": True
        }
    finally:
        release.set()
        holder.join(1)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)
    monkeypatch.setattr(lsp_process, "_release_lifecycle", release_lifecycle)
    monkeypatch.setattr(lsp_process, "_prepare_generation", prepare_generation)
    _exercise_explicit_restart_retirement_deadline_finishes_without_caller_retry(
        monkeypatch, tmp_path
    )


def _exercise_explicit_restart_retirement_deadline_finishes_without_caller_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(
        tmp_path,
        "--lifecycle",
        "--spawn-descendant",
        "--sleep-seconds",
        "30",
    )
    coordinator = process._coordinator
    generation = coordinator.active
    recovery = coordinator.recovery_thread
    assert generation is not None
    assert generation.tree is not None
    assert recovery is not None
    tree = generation.tree
    terminate = lsp_process.ProcessTree.terminate
    fresh_cleanup_started = threading.Event()
    allow_fresh_cleanup = threading.Event()
    caller_deadline = time.monotonic() + 0.05

    def deadline_sensitive_terminate(current: object, *, deadline: float) -> None:
        if current is tree and deadline <= caller_deadline:
            threading.Event().wait(max(0.0, deadline - time.monotonic()) + 0.005)
            raise TimeoutError("restart caller retirement deadline expired")
        if current is tree:
            fresh_cleanup_started.set()
            if not allow_fresh_cleanup.wait(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("fresh autonomous cleanup stayed blocked")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "terminate",
        deadline_sensitive_terminate,
    )
    try:
        with pytest.raises(
            TimeoutError, match="restart caller retirement deadline expired"
        ):
            process.restart(caller_deadline)

        assert coordinator.recovery_request_pending.is_set()
        assert coordinator.recovery_request_nonce == generation.nonce
        assert _coordinator_wait(
            process,
            lambda: fresh_cleanup_started.is_set()
            or coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=2,
        )

        allow_fresh_cleanup.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(1)

        assert process.restart_count == 0
        assert process.state is ProcessState.FAILED
        assert not recovery.is_alive()
        assert coordinator.recovery_request_nonce is None
        assert not coordinator.recovery_request_pending.is_set()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        allow_fresh_cleanup.set()
        monkeypatch.setattr(lsp_process.ProcessTree, "terminate", terminate)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def _exercise_explicit_restart_candidate_cleanup_replaces_stopped_recovery_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = LspProcess.start(
        _command("--lifecycle", "--sleep-seconds", "30"),
        cwd=tmp_path,
        owner_root=tmp_path / ("b" * 32),
    )
    coordinator = process._coordinator
    original_recovery = coordinator.recovery_thread
    owner = coordinator.owner_directory
    assert original_recovery is not None and owner is not None
    remove_lease = lsp_process._OwnerDirectory.remove_lease
    retry_started = threading.Event()
    allow_retry = threading.Event()
    retry_threads: list[threading.Thread] = []
    remove_calls = 0

    def fail_restart_spawn(
        _cls: type[lsp_process.ProcessTree],
        *_args: object,
        **_kwargs: object,
    ) -> lsp_process.ProcessTree:
        raise OSError("restart candidate spawn failed")

    def transient_remove_lease(current: lsp_process._OwnerDirectory) -> None:
        nonlocal remove_calls
        if current is owner:
            remove_calls += 1
            if remove_calls == 1:
                raise OSError("restart cleanup lease removal failed")
            retry_threads.append(threading.current_thread())
            retry_started.set()
            assert allow_retry.wait(5)
        remove_lease(current)

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(fail_restart_spawn),
    )
    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "remove_lease",
        transient_remove_lease,
    )
    try:
        with pytest.raises(OSError, match="restart candidate spawn failed"):
            process.restart(time.monotonic() + 5)

        assert coordinator.recovery_request_pending.is_set()
        assert retry_started.wait(2)
        assert len(retry_threads) == 1
        replacement = retry_threads[0]
        assert replacement is not original_recovery
        assert replacement.is_alive()

        allow_retry.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        replacement.join(1)

        assert remove_calls == 2
        assert not replacement.is_alive()
        assert not coordinator.recovery_request_pending.is_set()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        allow_retry.set()
        monkeypatch.setattr(
            lsp_process._OwnerDirectory,
            "remove_lease",
            remove_lease,
        )
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def _exercise_recovery_owned_candidate_failure_retains_worker_for_terminal_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = LspProcess.start(
        _command("--lifecycle", "--sleep-seconds", "30"),
        cwd=tmp_path,
        owner_root=tmp_path / ("c" * 32),
    )
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    owner = coordinator.owner_directory
    assert recovery is not None and owner is not None
    caller = threading.current_thread()
    restart_generation = lsp_process._restart_generation
    remove_lease = lsp_process._OwnerDirectory.remove_lease
    retry_started = threading.Event()
    allow_retry = threading.Event()
    retry_threads: list[threading.Thread] = []
    remove_calls = 0

    def expire_caller_restart(instance: LspProcess, deadline: float) -> None:
        if threading.current_thread() is caller:
            raise TimeoutError("explicit restart caller deadline expired")
        restart_generation(instance, deadline)

    def fail_restart_spawn(
        _cls: type[lsp_process.ProcessTree],
        *_args: object,
        **_kwargs: object,
    ) -> lsp_process.ProcessTree:
        raise OSError("recovery candidate spawn failed")

    def transient_remove_lease(current: lsp_process._OwnerDirectory) -> None:
        nonlocal remove_calls
        if current is owner:
            remove_calls += 1
            if remove_calls == 1:
                raise OSError("recovery cleanup lease removal failed")
            retry_threads.append(threading.current_thread())
            retry_started.set()
            assert allow_retry.wait(5)
        remove_lease(current)

    monkeypatch.setattr(lsp_process, "_restart_generation", expire_caller_restart)
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(fail_restart_spawn),
    )
    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "remove_lease",
        transient_remove_lease,
    )
    try:
        with pytest.raises(TimeoutError, match="explicit restart caller deadline expired"):
            process.restart(time.monotonic() + 0.05)

        assert retry_started.wait(2)
        assert retry_threads == [recovery]
        assert recovery.is_alive()

        allow_retry.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(1)

        assert remove_calls == 2
        assert not recovery.is_alive()
        assert not coordinator.recovery_request_pending.is_set()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        allow_retry.set()
        monkeypatch.setattr(
            lsp_process._OwnerDirectory,
            "remove_lease",
            remove_lease,
        )
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_terminal_intent_lock_respects_deadline_and_cannot_commit_success(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    lsp_process._stop_recovery_owner(coordinator, time.monotonic() + 1)
    assert lsp_process._queue_owner_failure(coordinator, "injected pending failure")
    held = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def hold_terminal_intent() -> None:
        coordinator.terminal_intent_lock.acquire()
        try:
            held.set()
            assert release.wait(2)
        finally:
            coordinator.terminal_intent_lock.release()

    def shutdown() -> None:
        try:
            process.shutdown(time.monotonic() + 0.05)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    holder = threading.Thread(target=hold_terminal_intent)
    holder.start()
    assert held.wait(1)
    closer = threading.Thread(target=shutdown)
    closer.start()
    completed_before_release = finished.wait(0.2)
    try:
        assert completed_before_release is True
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
        assert coordinator.pending_failure_intents == 1
        assert coordinator.success_committed is False
        assert (process.owner_root / "lease.json").is_file()
        assert lsp_process._coordinator_has_ownership(coordinator)
    finally:
        release.set()
        holder.join(1)
        closer.join(2)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)

    assert coordinator.terminal_outcome == "failure"
    assert (process.owner_root / "failure.json").is_file()


@pytest.mark.parametrize("worker_prefix", ["lsp-recovery-", "lsp-heartbeat-"])
def test_startup_worker_start_past_original_deadline_never_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_prefix: str,
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_start = threading.Thread.start
    returned: list[LspProcess] = []
    error: BaseException | None = None

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def delayed_start(thread: threading.Thread) -> None:
        if thread.name.startswith(worker_prefix):
            time.sleep(lsp_process._STARTUP_WAIT_SECONDS + 0.05)
        real_start(thread)

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen)
    monkeypatch.setattr(threading.Thread, "start", delayed_start)
    started = time.monotonic()
    try:
        error = _capture_start_error(returned, tmp_path)
    finally:
        for process in returned:
            process.close(time.monotonic() + 5)
        _drive_retained_cleanup(error)

    assert time.monotonic() - started >= lsp_process._STARTUP_WAIT_SECONDS
    assert returned == []
    assert error is not None
    assert any(isinstance(item, TimeoutError) for item in _cause_chain(error))
    settle_deadline = time.monotonic() + 2
    _await_children_reaped(children, settle_deadline)
    assert children and _all_children_reaped(children)


def test_startup_terminal_intent_timeout_returns_retryable_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "startup-terminal-intent-descendant.pid"
    held = threading.Event()
    release = threading.Event()
    captured: list[LspProcess] = []
    holders: list[threading.Thread] = []

    def fail_with_terminal_intent_lock(
        instance: LspProcess,
        deadline: float | None = None,
    ) -> None:
        coordinator = instance._coordinator

        def hold_terminal_intent() -> None:
            coordinator.terminal_intent_lock.acquire()
            try:
                held.set()
                release.wait(10)
            finally:
                coordinator.terminal_intent_lock.release()

        holder = threading.Thread(target=hold_terminal_intent)
        holders.append(holder)
        captured.append(instance)
        holder.start()
        assert held.wait(1)
        assert deadline is not None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining + 0.01)
        raise RuntimeError("startup failed while terminal intent lock stayed held")

    monkeypatch.setattr(
        lsp_process, "_start_lifecycle_workers", fail_with_terminal_intent_lock
    )
    owner_root = tmp_path / OWNER_NONCE
    coordinator: lsp_process._LifecycleCoordinator | None = None
    cleanup_error: lsp_process.StartupCleanupError | None = None

    try:
        with pytest.raises(lsp_process.StartupCleanupError) as raised:
            _start(
                tmp_path,
                "--lifecycle",
                "--spawn-descendant",
                "--descendant-pid-file",
                str(pid_file),
            )

        cleanup_error = raised.value
        assert len(captured) == 1
        instance = captured[0]
        coordinator = instance._coordinator
        generation_nonce = instance.generation_nonce
        server_pid = instance.process.pid
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert callable(raised.value.retry_cleanup)
        assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert coordinator.cleanup_result.ownership_pending is True
        assert "TimeoutError" in _cleanup_error_types(coordinator)
        assert lsp_process._coordinator_has_ownership(coordinator)
        assert coordinator in lsp_process._pending_startup_cleanup_snapshot()
        assert owner_root.is_dir()
        assert (owner_root / "owner.json").is_file()
        assert (owner_root / "lease.json").is_file()

        _await_process_exit(instance.process)
        assert instance.process.poll() is not None
        assert pid_file.is_file()
        descendant = int(pid_file.read_text(encoding="ascii"))
        _await_pid_exit(descendant)
        assert not _pid_alive(descendant)

        release.set()
        holders[0].join(1)
        raised.value.retry_cleanup(time.monotonic() + 5)

        failure = json.loads((owner_root / "failure.json").read_bytes())
        lsp_process._validate_failure_record(
            failure,
            code="startup_failed",
            owner_nonce=OWNER_NONCE,
            generation_nonce=generation_nonce,
            pid=server_pid,
        )
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
        assert owner_root.is_dir()
        assert not (owner_root / "lease.json").exists()
        assert coordinator not in lsp_process._pending_startup_cleanup_snapshot()
    finally:
        release.set()
        for holder in holders:
            holder.join(1)
        if coordinator is None and captured:
            coordinator = captured[0]._coordinator
        _retry_owned_cleanup(coordinator, cleanup_error)


def test_autonomous_restart_failure_with_expired_transition_lock_is_sticky(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    assert recovery is not None and heartbeat is not None
    generation = coordinator.active
    assert generation is not None
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    graceful_cleanup_seconds = lsp_process._GRACEFUL_CLEANUP_SECONDS
    restart_generation = lsp_process._restart_generation
    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 0.05)
    held = threading.Event()
    release = threading.Event()
    terminal_attempted = threading.Event()
    holders: list[threading.Thread] = []
    remember_background_error = lsp_process._remember_background_cleanup_error

    def fail_restart(_instance: LspProcess, deadline: float) -> None:
        def hold_transition() -> None:
            with coordinator.condition:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold_transition)
        holders.append(holder)
        holder.start()
        assert held.wait(1)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining + 0.01)
        raise RuntimeError("autonomous restart failed with transition lock held")

    def observe_background_error(
        current: lsp_process._LifecycleCoordinator,
        error: BaseException,
    ) -> None:
        remember_background_error(current, error)
        if current is coordinator:
            terminal_attempted.set()

    monkeypatch.setattr(lsp_process, "_restart_generation", fail_restart)
    monkeypatch.setattr(
        lsp_process, "_remember_background_cleanup_error", observe_background_error
    )

    try:
        assert lsp_process._queue_generation_failure(
            coordinator, generation, "injected autonomous restart failure"
        )
        assert terminal_attempted.wait(2)

        assert len(holders) == 1
        assert coordinator.mandatory_failure_intent == lsp_process._FailureEvidenceIdentity(
            "restart_failed",
            process.owner_nonce,
            generation_nonce,
            server_pid,
        )
        assert coordinator.phase is not lsp_process._LifecyclePhase.STOPPED_SUCCESS
        assert lsp_process._coordinator_has_ownership(coordinator)
        assert process.owner_root.is_dir()
        assert (process.owner_root / "owner.json").is_file()
        assert (process.owner_root / "lease.json").is_file()
        assert recovery.is_alive()
        assert heartbeat.is_alive()

        release.set()
        holders[0].join(1)

        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(1)
        heartbeat.join(1)

        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        lsp_process._validate_failure_record(
            failure,
            code="restart_failed",
            owner_nonce=process.owner_nonce,
            generation_nonce=generation_nonce,
            pid=server_pid,
        )
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
        assert process.owner_root.is_dir()
        assert not (process.owner_root / "lease.json").exists()
        assert not recovery.is_alive()
        assert not heartbeat.is_alive()
        assert not lsp_process._coordinator_has_ownership(coordinator)
    finally:
        release.set()
        for holder in holders:
            holder.join(1)
        _close_owned_process(process, coordinator)
    monkeypatch.setattr(
        lsp_process, "_GRACEFUL_CLEANUP_SECONDS", graceful_cleanup_seconds
    )
    monkeypatch.setattr(lsp_process, "_restart_generation", restart_generation)
    monkeypatch.setattr(
        lsp_process, "_remember_background_cleanup_error", remember_background_error
    )
    _exercise_recovery_owned_candidate_failure_retains_worker_for_terminal_retry(
        monkeypatch, tmp_path
    )


def test_persistent_autonomous_cleanup_fault_retries_with_fresh_bounded_budgets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 0.02)
    monkeypatch.setattr(lsp_process, "_RECOVERY_RETRY_SECONDS", 0.04, raising=False)
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    generation = coordinator.active
    assert None not in (recovery, heartbeat, generation)
    held = threading.Event()
    release = threading.Event()
    first_cleanup = threading.Event()
    terminal_attempts: list[tuple[float, float]] = []
    terminal_failure = lsp_process._terminal_failure_lsp_process

    def hold_transition() -> None:
        with coordinator.condition:
            held.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_transition)

    def fail_restart(_instance: LspProcess, deadline: float) -> None:
        holder.start()
        assert held.wait(1)
        threading.Event().wait(max(0.0, deadline - time.monotonic()) + 0.005)
        raise RuntimeError("persistent autonomous restart failure")

    def observe_terminal_failure(
        instance: LspProcess,
        code: str,
        deadline: float,
    ) -> None:
        terminal_attempts.append((time.monotonic(), deadline))
        first_cleanup.set()
        terminal_failure(instance, code, deadline)

    monkeypatch.setattr(lsp_process, "_restart_generation", fail_restart)
    monkeypatch.setattr(
        lsp_process,
        "_terminal_failure_lsp_process",
        observe_terminal_failure,
    )

    try:
        assert lsp_process._queue_generation_failure(
            coordinator, generation, "injected persistent restart failure"
        )
        assert first_cleanup.wait(1)
        # Retries are spaced by the recovery interval, so a fixed wait counts
        # how fast the machine is rather than how the retry behaves. Wait for
        # the second attempt instead, and bound the count by the time actually
        # spent waiting, which still catches a runaway loop.
        waiting_since = time.monotonic()
        assert _poll_until(lambda: len(terminal_attempts) >= 2, 5)
        elapsed = time.monotonic() - waiting_since
        attempts = list(terminal_attempts)
        assert len(attempts) <= int(elapsed / lsp_process._RECOVERY_RETRY_SECONDS) + 3
        assert _all_deadlines_ahead(attempts)
        assert recovery.is_alive()
        assert heartbeat.is_alive()
        assert coordinator.recovery_thread is recovery
        assert coordinator.heartbeat_thread is heartbeat
        assert coordinator.cleanup_result.ownership_pending is True
        assert (process.owner_root / "lease.json").is_file()

        release.set()
        holder.join(1)
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        assert not (process.owner_root / "lease.json").exists()
    finally:
        release.set()
        holder.join(1)
        # The injected fault and the 0.02 s cleanup budget are the subject of
        # the test, not of its teardown. Undo them first, and tolerate the
        # background failure the coordinator already recorded — on a slow
        # machine `close` re-raises it and buries the real result.
        monkeypatch.undo()
        with contextlib.suppress(RuntimeError):
            _close_if_owned(process, coordinator)


def test_second_explicit_restart_failure_with_held_transition_lock_is_sticky(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    process.restart(time.monotonic() + 5)
    coordinator = process._coordinator
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    held = threading.Event()
    seize = threading.Event()
    release = threading.Event()
    restart_thread = threading.current_thread()
    release_lifecycle = lsp_process._release_lifecycle
    armed = True

    def hold_transition() -> None:
        assert seize.wait(2)
        with coordinator.condition:
            held.set()
            release.wait(10)

    def release_then_contend(current: lsp_process._LifecycleCoordinator) -> None:
        nonlocal armed
        release_lifecycle(current)
        if current is coordinator and armed and threading.current_thread() is restart_thread:
            armed = False
            seize.set()
            assert held.wait(1)

    holder = threading.Thread(target=hold_transition)
    holder.start()
    monkeypatch.setattr(lsp_process, "_release_lifecycle", release_then_contend)

    try:
        with pytest.raises(TimeoutError, match="lifecycle transition lock"):
            process.restart(time.monotonic() + 0.1)

        assert coordinator.mandatory_failure_intent == lsp_process._FailureEvidenceIdentity(
            "process_exited",
            process.owner_nonce,
            generation_nonce,
            server_pid,
        )
        assert coordinator.phase is not lsp_process._LifecyclePhase.STOPPED_SUCCESS
        assert lsp_process._coordinator_has_ownership(coordinator)

        release.set()
        holder.join(1)
        process.close(time.monotonic() + 5)

        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        lsp_process._validate_failure_record(
            failure,
            code="process_exited",
            owner_nonce=process.owner_nonce,
            generation_nonce=generation_nonce,
            pid=server_pid,
        )
        assert coordinator.phase is lsp_process._LifecyclePhase.STOPPED_FAILURE
        assert process.owner_root.is_dir()
        assert not (process.owner_root / "lease.json").exists()
    finally:
        release.set()
        holder.join(1)
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_heartbeat_refreshes_lease_while_tree_cleanup_driver_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process, "_LEASE_EXPIRY_SECONDS", 0.06)
    monkeypatch.setattr(lsp_process, "_GRACEFUL_CLEANUP_SECONDS", 0.05)
    pid_file = tmp_path / "blocked-cleanup-descendant.pid"
    process = _start(
        tmp_path,
        "--lifecycle",
        "--ignore-shutdown",
        "--spawn-descendant",
        "--descendant-pid-file",
        str(pid_file),
    )
    descendant = _await_descendant_pid(pid_file)
    coordinator = process._coordinator
    lsp_process._stop_recovery_owner(coordinator, time.monotonic() + 1)
    generation = coordinator.active
    assert generation is not None
    assert generation.tree is not None
    tree = generation.tree
    terminate = lsp_process.ProcessTree.terminate
    entered = threading.Event()
    release = threading.Event()
    cleanup_errors: list[BaseException] = []
    heartbeat_records: list[dict[str, object]] = []
    lease_path = process.owner_root / "lease.json"
    initial = json.loads(lease_path.read_bytes())
    write_lease = lsp_process._OwnerDirectory.write_lease

    def observe_heartbeat(current: object, record: object, **kwargs: object) -> None:
        assert isinstance(record, dict)
        if threading.current_thread() is coordinator.heartbeat_thread:
            heartbeat_records.append(dict(record))
        write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    def blocked_terminate(current: object, *, deadline: float) -> None:
        if current is tree and not release.is_set():
            entered.set()
            release.wait(max(0.0, deadline - time.monotonic()))
            raise TimeoutError("tree termination stayed blocked")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    def fail_terminally() -> None:
        try:
            process._terminal_failure("injected_failure", time.monotonic() + 2)
        except BaseException as error:
            cleanup_errors.append(error)

    monkeypatch.setattr(lsp_process.ProcessTree, "terminate", blocked_terminate)
    monkeypatch.setattr(
        lsp_process._OwnerDirectory, "write_lease", observe_heartbeat
    )
    monkeypatch.setattr(lsp_process, "_HEARTBEAT_SECONDS", 0.02)
    coordinator.heartbeat_wake.set()
    cleanup = threading.Thread(target=fail_terminally)
    cleanup.start()
    assert entered.wait(1), cleanup_errors
    _await_records(heartbeat_records, 2, 0.3)
    lsp_process._acquire_lease(coordinator, time.monotonic() + 1)
    try:
        during = json.loads(lease_path.read_bytes())
    finally:
        lsp_process._release_lease(coordinator)
    descendant_live = _pid_alive(descendant)
    try:
        assert len(heartbeat_records) >= 2, (
            coordinator.heartbeat_thread,
            coordinator.heartbeat_stop.is_set(),
            coordinator.background_cleanup_error,
            coordinator.phase,
        )
        assert during["heartbeat_at"] != initial["heartbeat_at"]
        assert descendant_live is True
        assert coordinator.cleanup_result.ownership_pending is True
    finally:
        release.set()
        cleanup.join(2)
        monkeypatch.setattr(lsp_process.ProcessTree, "terminate", terminate)
        monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", write_lease)
        process.close(time.monotonic() + 5)

    assert not cleanup.is_alive()
    assert any("tree termination stayed blocked" in str(error) for error in cleanup_errors)
    assert not _pid_alive(descendant)


def _exercise_mock_windows_artifact_write(
    owner: lsp_process._OwnerDirectory,
    artifact: str,
) -> None:
    if artifact == "owner":
        owner.write_record("owner.json", {"state": "running"})
    elif artifact == "failure":
        owner.write_record("failure.json", {"code": "injected"})
    elif artifact == "lease":
        owner.write_lease({"state": "live"})
    else:  # pragma: no cover - parametrization is closed below
        raise AssertionError(artifact)


def _mock_windows_artifact_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    close_handle: object,
    create_calls: list[str] | None = None,
) -> lsp_process._OwnerDirectory:
    workspace = lsp_process._windows_workspace
    owner = lsp_process._OwnerDirectory(
        tmp_path / OWNER_NONCE,
        parent_handle=101,
        parent_identity=(1, b"parent", True),
        owner_handle=202,
        owner_identity=(1, b"owner", True),
        owner_permissions_verified=True,
    )
    identity = (1, b"artifact", False)
    def create_file(_parent: int, name: str) -> int:
        if create_calls is not None:
            create_calls.append(name)
        return 303

    monkeypatch.setattr(workspace, "create_file", create_file)
    monkeypatch.setattr(workspace, "identity", lambda _handle, directory: identity)
    monkeypatch.setattr(workspace, "write_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(workspace, "flush_file", lambda _handle: None)
    monkeypatch.setattr(workspace, "flush_directory", lambda _handle: True)
    monkeypatch.setattr(workspace, "publish_file", lambda *_args: None)
    monkeypatch.setattr(workspace, "replace_file", lambda *_args: None)
    monkeypatch.setattr(
        workspace,
        "open_deletable_file",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError),
    )
    monkeypatch.setattr(workspace, "delete_handle", lambda _handle: None)
    monkeypatch.setattr(workspace, "close_handle", close_handle)
    return owner


@pytest.mark.skipif(os.name != "nt", reason="Windows native create boundary")
@pytest.mark.parametrize("artifact", ["failure", "lease"])
def test_windows_post_create_identity_failure_retains_one_temp_intent_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process._OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    workspace = lsp_process._windows_workspace
    real_create = workspace.create_file
    real_identity = workspace.identity
    real_delete = workspace.delete_handle
    create_active = False
    cleanup_allowed = False
    create_calls: list[str] = []
    temporary_prefix = ".evidence-" if artifact == "failure" else ".lease-"

    def identity(handle: int, *, directory: bool) -> object:
        if create_active and not directory:
            raise OSError("temporary identity validation failed")
        return real_identity(handle, directory=directory)

    def create_file(parent: int, name: str) -> int:
        nonlocal create_active
        create_calls.append(name)
        create_active = True
        try:
            return real_create(parent, name)
        finally:
            create_active = False

    def delete_handle(handle: int) -> None:
        if not cleanup_allowed:
            raise OSError("temporary cleanup blocked")
        real_delete(handle)

    monkeypatch.setattr(workspace, "identity", identity)
    monkeypatch.setattr(workspace, "create_file", create_file)
    monkeypatch.setattr(workspace, "delete_handle", delete_handle)

    try:
        errors: list[str] = []
        for _attempt in range(2):
            with pytest.raises(OSError) as raised:
                _exercise_mock_windows_artifact_write(owner, artifact)
            errors.append(str(raised.value))

        assert errors == ["temporary cleanup blocked"] * 2
        assert len(create_calls) == 1
        pending = list(owner._pending_temp_names)
        assert pending == create_calls
        assert _entry_names(owner_root) == {
            "cancellation",
            pending[0],
        }

        with pytest.raises(OSError, match="temporary cleanup blocked"):
            owner.close()
        assert owner._pending_temp_names == pending
        assert owner.owner_handle is not None
        assert owner.parent_handle >= 0
        assert owner._closed is False

        cleanup_allowed = True
        owner._retry_pending_temp_names()
        assert owner._pending_temp_names == []
        assert _entry_names(owner_root) == {"cancellation"}

        owner.remove_success_scratch()
        owner.close()
        assert owner._closed is True
        assert not owner_root.exists()
    finally:
        cleanup_allowed = True
        _remove_temporaries(owner_root, f"{temporary_prefix}*.tmp")
        if not owner._closed:
            owner.remove_success_scratch()
            owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native create boundary")
@pytest.mark.parametrize("artifact", ["failure", "lease"])
def test_windows_precreate_failure_proves_absence_and_clears_temp_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process._OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    workspace = lsp_process._windows_workspace
    real_open = workspace.open_deletable_file
    attempted_names: list[str] = []
    cleanup_probes: list[str] = []

    def fail_before_create(_parent: int, name: str) -> int:
        attempted_names.append(name)
        raise OSError("temporary pre-create failure")

    def open_deletable_file(parent: int, name: str) -> int:
        cleanup_probes.append(name)
        return real_open(parent, name)

    monkeypatch.setattr(workspace, "create_file", fail_before_create)
    monkeypatch.setattr(workspace, "open_deletable_file", open_deletable_file)

    try:
        with pytest.raises(OSError, match="temporary pre-create failure"):
            _exercise_mock_windows_artifact_write(owner, artifact)

        assert len(attempted_names) == 1
        assert cleanup_probes == attempted_names
        assert owner._pending_temp_names == []
        assert _entry_names(owner_root) == {"cancellation"}

        owner.remove_success_scratch()
        owner.close()
        assert owner._closed is True
        assert not owner_root.exists()
    finally:
        if not owner._closed:
            owner.remove_success_scratch()
            owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows child-handle retry boundary")
@pytest.mark.parametrize("artifact", ["owner", "failure", "lease"])
@pytest.mark.parametrize("published", [False, True])
def test_windows_child_close_failure_is_retained_and_retried_before_owner_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
    published: bool,
) -> None:
    close_calls: list[int] = []
    child_close_attempts = 0

    def close_handle(handle: int) -> None:
        nonlocal child_close_attempts
        close_calls.append(handle)
        if handle == 303:
            child_close_attempts += 1
            if child_close_attempts == 1:
                raise OSError("child handle close failed")

    owner = _mock_windows_artifact_owner(monkeypatch, tmp_path, close_handle)
    if not published:
        monkeypatch.setattr(
            lsp_process._windows_workspace,
            "write_all",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("artifact write failed")
            ),
        )

    with pytest.raises(OSError, match="child handle close failed"):
        _exercise_mock_windows_artifact_write(owner, artifact)

    assert owner._pending_child_handles == [303]
    owner.close()
    assert close_calls == [303, 303, 202, 101]
    assert owner._pending_child_handles == []
    assert owner._closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows child-handle retry boundary")
@pytest.mark.parametrize("artifact", ["owner", "failure", "lease"])
@pytest.mark.parametrize("published", [False, True])
def test_persistent_windows_child_close_failure_blocks_owner_release_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
    published: bool,
) -> None:
    child_close_allowed = False
    close_calls: list[int] = []
    create_calls: list[str] = []

    def close_handle(handle: int) -> None:
        close_calls.append(handle)
        if handle == 303 and not child_close_allowed:
            raise OSError("child handle close still failed")

    owner = _mock_windows_artifact_owner(
        monkeypatch, tmp_path, close_handle, create_calls
    )
    if not published:
        monkeypatch.setattr(
            lsp_process._windows_workspace,
            "write_all",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("artifact write failed")
            ),
        )
    with pytest.raises(OSError, match="child handle close still failed"):
        _exercise_mock_windows_artifact_write(owner, artifact)
    with pytest.raises(OSError, match="child handle close still failed"):
        _exercise_mock_windows_artifact_write(owner, artifact)
    with pytest.raises(OSError, match="child handle close still failed"):
        owner.close()

    assert owner.owner_handle == 202
    assert owner.parent_handle == 101
    assert owner._pending_child_handles == [303]
    assert len(create_calls) == 1
    assert close_calls == [303, 303, 303]
    child_close_allowed = True
    owner.close()
    assert close_calls == [303, 303, 303, 303, 202, 101]
    assert owner._pending_child_handles == []
    assert owner._closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows child-handle retry boundary")
def test_multiple_windows_child_close_failures_are_all_retained_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_close_allowed = False
    close_calls: list[int] = []

    def close_handle(handle: int) -> None:
        close_calls.append(handle)
        if handle in {303, 404} and not child_close_allowed:
            raise OSError(f"child handle {handle} close failed")

    owner = _mock_windows_artifact_owner(monkeypatch, tmp_path, close_handle)

    with pytest.raises(OSError, match="303"):
        owner._close_child_handles(303, 404)

    assert close_calls == [303, 404]
    assert owner._pending_child_handles == [303, 404]
    child_close_allowed = True
    owner.close()
    assert close_calls == [303, 404, 303, 404, 202, 101]
    assert owner._pending_child_handles == []
    assert owner._closed is True


def test_first_terminal_identity_survives_later_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    heartbeat = coordinator.heartbeat_thread
    assert owner is not None and heartbeat is not None
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    attempted = threading.Event()
    real_write_lease = lsp_process._OwnerDirectory.write_lease

    def fail_heartbeat(current: object, record: object, **kwargs: object) -> None:
        if current is owner and threading.current_thread() is heartbeat:
            attempted.set()
            raise OSError("later heartbeat failed")
        real_write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    lsp_process._mark_terminal_failure(
        process,
        coordinator,
        "injected_failure",
        time.monotonic() + 1,
    )
    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", fail_heartbeat)
    coordinator.heartbeat_wake.set()
    assert attempted.wait(1)
    heartbeat.join(2)
    monkeypatch.setattr(lsp_process._OwnerDirectory, "write_lease", real_write_lease)
    process.close(time.monotonic() + 5)

    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert coordinator.terminal_code == "injected_failure"
    assert failure["code"] == "injected_failure"
    assert failure["generation_nonce"] == generation_nonce
    assert failure["server_pid"] == server_pid


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush boundary")
@pytest.mark.parametrize("artifact", ["failure", "lease"])
def test_windows_artifact_directory_flush_false_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
) -> None:
    owner = _mock_windows_artifact_owner(monkeypatch, tmp_path, lambda _handle: None)
    flushes: list[int] = []
    deleted: list[int] = []

    def reject_directory_flush(handle: int) -> bool:
        flushes.append(handle)
        return False

    monkeypatch.setattr(
        lsp_process._windows_workspace,
        "flush_directory",
        reject_directory_flush,
    )
    monkeypatch.setattr(
        lsp_process._windows_workspace,
        "delete_handle",
        lambda handle: deleted.append(handle),
    )

    with pytest.raises(OSError, match="directory.*flush"):
        _exercise_mock_windows_artifact_write(owner, artifact)

    assert flushes == [202]
    assert deleted == []
    owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory flush boundary")
def test_windows_failure_flush_false_retains_cleanup_until_barrier_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    owner = coordinator.owner_directory
    assert owner is not None and owner.owner_handle is not None
    real_flush = lsp_process._windows_workspace.flush_directory
    allow_flush = False
    attempts = 0

    def controlled_flush(handle: int) -> bool:
        nonlocal attempts
        if handle == owner.owner_handle:
            attempts += 1
            if not allow_flush:
                return False
        return real_flush(handle)

    monkeypatch.setattr(
        lsp_process._windows_workspace,
        "flush_directory",
        controlled_flush,
    )
    try:
        with pytest.raises(OSError, match="directory.*flush"):
            process._terminal_failure("injected_failure", time.monotonic() + 5)

        failure = process.owner_root / "failure.json"
        assert failure.is_file()
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
        assert coordinator.owner_directory is owner
        assert process.state is ProcessState.DEGRADED
        assert (process.owner_root / "lease.json").is_file()
        assert ("evidence", "OSError") in _cleanup_error_steps(coordinator)
        attempts_before_retry = attempts

        allow_flush = True
        process.close(time.monotonic() + 5)

        assert attempts > attempts_before_retry
        assert process.state is ProcessState.FAILED
        assert coordinator.cleanup_result.evidence == "success"
        assert not (process.owner_root / "lease.json").exists()
    finally:
        allow_flush = True
        if lsp_process._coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync boundary")
@pytest.mark.parametrize("persistent", [False, True], ids=["one-shot", "persistent"])
def test_posix_failure_retry_repeats_directory_fsync_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persistent: bool,
) -> None:
    owner_root = tmp_path / OWNER_NONCE
    owner = lsp_process._OwnerDirectory.open(owner_root)
    owner.create(time.monotonic() + 1)
    assert owner.owner_handle is not None
    owner_handle = owner.owner_handle
    identity = lsp_process._FailureEvidenceIdentity(
        "injected_failure",
        OWNER_NONCE,
        "b" * 32,
        4242,
    )
    coordinator = lsp_process._LifecycleCoordinator(
        owner,
        startup_generation_nonce=identity.generation_nonce,
    )
    coordinator.terminal_outcome = "failure"
    coordinator.terminal_code = identity.code
    coordinator.failure_evidence_identity = identity
    coordinator.phase = lsp_process._LifecyclePhase.STOPPING_FAILURE
    real_fsync = lsp_process.os.fsync
    keep_failing = persistent
    attempts = 0

    def controlled_fsync(descriptor: int) -> None:
        nonlocal attempts
        if descriptor == owner_handle:
            attempts += 1
            if keep_failing or attempts == 1:
                raise OSError("owner directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(lsp_process.os, "fsync", controlled_fsync)
    try:
        with pytest.raises(OSError, match="owner directory fsync failed"):
            lsp_process._ensure_failure_evidence(
                None,
                coordinator,
                identity.code,
                time.monotonic() + 1,
            )

        assert attempts == 1
        assert coordinator.cleanup_result.evidence != "success"
        assert (owner_root / "failure.json").is_file()
        if persistent:
            with pytest.raises(OSError, match="owner directory fsync failed"):
                lsp_process._ensure_failure_evidence(
                    None,
                    coordinator,
                    identity.code,
                    time.monotonic() + 1,
            )
            assert attempts == 2
            assert coordinator.cleanup_result.evidence != "success"
            keep_failing = False

        lsp_process._ensure_failure_evidence(
            None,
            coordinator,
            identity.code,
            time.monotonic() + 1,
        )
        assert attempts == (3 if persistent else 2)
        assert coordinator.cleanup_result.evidence == "success"
        assert not list(owner_root.glob(".evidence-*.tmp"))
    finally:
        monkeypatch.setattr(lsp_process.os, "fsync", real_fsync)
        owner.close()
