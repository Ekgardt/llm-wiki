"""Process ownership and bounded transport tests for LSP children."""

from __future__ import annotations

import dataclasses
import io
import json
import math
import os
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


@pytest.fixture(autouse=True)
def _no_lsp_lifecycle_owner_leaks(monkeypatch: pytest.MonkeyPatch):
    started: list[LspProcess] = []
    start = LspProcess.start.__func__

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
    existing = {
        id(thread)
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-")
    }
    yield
    owned = [
        process.owner_nonce
        for process in started
        if lsp_process._coordinator_has_ownership(process._coordinator)
    ]
    leaked = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-") and id(thread) not in existing
    ]
    assert owned == []
    assert leaked == []


def _command(*arguments: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *arguments]


def _start(tmp_path: Path, *arguments: str) -> LspProcess:
    return LspProcess.start(
        _command(*arguments), cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE
    )


def _wait(process: LspProcess, seconds: float = 10) -> int:
    return process.wait_for_exit(time.monotonic() + seconds)


def _expect_active_generation_exit(process: LspProcess) -> None:
    generation = process._coordinator.active
    assert generation is not None
    generation.expected_exit.set()


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
        return None


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


def _wait_for_owned_pids_to_exit(
    process: LspProcess,
    pids: list[int],
    *,
    timeout: float = 2.0,
) -> None:
    coordinator = process._coordinator
    deadline = time.monotonic() + timeout
    while True:
        live = [pid for pid in pids if _pid_alive(pid)]
        if not live:
            return
        with coordinator.condition:
            generations = lsp_process._generations_locked(coordinator)
            assert any(generation.tree is not None for generation in generations), live
            owner = coordinator.owner_directory
            assert owner is not None and not owner._closed, live
            assert (process.owner_root / "lease.json").is_file(), live
        if time.monotonic() >= deadline:
            pytest.fail(f"owned LSP PIDs did not exit before deadline: {live}")
        time.sleep(0.01)


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
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    descendant = int(pid_file.read_text(encoding="ascii"))
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
    descendant_deadline = time.monotonic() + 2
    while _pid_alive(descendant) and time.monotonic() < descendant_deadline:
        time.sleep(0.01)
    assert not _pid_alive(descendant)
    assert not process.owner_root.exists()
    assert all(thread is None or not thread.is_alive() for thread in owner_threads)


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


def test_idle_expiry_is_exactly_300_seconds_and_rejects_non_finite_input(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    try:
        assert process.idle_expired(process.last_used_monotonic + 299.999) is False
        assert process.idle_expired(process.last_used_monotonic + 300.0) is True
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
    wait_deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < wait_deadline:
        time.sleep(0.01)
    descendant = int(pid_file.read_text(encoding="ascii"))
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


def test_second_fatal_failure_is_terminal_and_retains_bounded_evidence(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--always-crash")

    with pytest.raises(ProtocolViolation):
        process.request("echo", {}, deadline=time.monotonic() + 5)

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
    assert (process.owner_root / "lease.json").is_file()
    assert process._coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
    process.close(time.monotonic() + 5)
    assert not (process.owner_root / "lease.json").exists()


def test_idle_fatal_restarts_once_then_second_idle_fatal_is_terminal(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "idle-exit-marker"
    process = _start(tmp_path, "--lifecycle", "--idle-exit-marker", str(marker))
    first_generation = process.generation_nonce
    deadline = time.monotonic() + 3

    lease_path = process.owner_root / "lease.json"
    while (
        (process.restart_count == 0 or not lease_path.exists())
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    assert process.restart_count == 1
    assert process.generation_nonce != first_generation
    lease = json.loads(lease_path.read_bytes())
    assert lease["generation_nonce"] == process.generation_nonce
    assert lease["server_pid"] == process.process.pid

    while (
        (
            process.state is not ProcessState.FAILED
            or process._tree is not None
            or (
                process._recovery_thread is not None
                and process._recovery_thread.is_alive()
            )
        )
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    assert process.state is ProcessState.FAILED
    assert process._tree is None
    assert (process.owner_root / "failure.json").is_file()
    assert lease_path.is_file()
    process.close(time.monotonic() + 5)
    assert not lease_path.exists()


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
    else:
        with pytest.raises(TimeoutError):
            process.request("ending", {}, deadline=time.monotonic() + 0.05)
        deadline = time.monotonic() + 3
        while process.restart_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.restart_count == 1
        process.shutdown(time.monotonic() + 5)

    if ending == "crash":
        deadline = time.monotonic() + 3
        while (
            (
                process.state is not ProcessState.FAILED
                or (
                    process._recovery_thread is not None
                    and process._recovery_thread.is_alive()
                )
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert process.state is ProcessState.FAILED
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
    wait_deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < wait_deadline:
        time.sleep(0.01)
    descendant = int(pid_file.read_text(encoding="ascii"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            process.request, "slow", {}, deadline=time.monotonic() + 5
        )
        deadline = time.monotonic() + 1
        while process.protocol.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
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
    assert set(environment) <= LSP_ENV_ALLOWLIST
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

    retained.released = True
    errors = lsp_process._drive_cleanup(
        None,
        time.monotonic() + 5,
        terminal=True,
        failure_code="startup_failed",
        coordinator_override=coordinator,
    )
    assert errors == []
    assert not lsp_process._coordinator_has_ownership(coordinator)


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


def test_start_passes_one_absolute_deadline_to_process_tree_and_protocol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deadlines: dict[str, float | None] = {}
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__
    real_protocol = lsp_process.LspProtocol

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
    monkeypatch.setattr(lsp_process, "LspProtocol", record_protocol)
    process = _start(tmp_path, "--lifecycle")
    try:
        assert deadlines["protocol"] == deadlines["spawn"]
    finally:
        process.close(time.monotonic() + 5)


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
        assert any(
            isinstance(item.error, TimeoutError)
            for item in coordinator.cleanup_result.errors
        )
        assert all(
            generation.process is None
            for generation in [
                coordinator.active,
                coordinator.candidate,
                *coordinator.retired,
            ]
            if generation is not None
        )
        assert coordinator.owner_directory is not None
        assert coordinator.owner_directory._closed is False
        assert (owner / "failure.json").is_file()
        assert (owner / "lease.json").is_file()
    finally:
        if captured:
            coordinator = captured[0]
            coordinator.heartbeat_stop.set()
            coordinator.heartbeat_wake.set()
            heartbeat = coordinator.heartbeat_thread
            if heartbeat is not None:
                heartbeat.join(1)
            owner_directory = coordinator.owner_directory
            if owner_directory is not None:
                try:
                    owner_directory.remove_lease()
                    owner_directory.close()
                except OSError:
                    pass


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

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(self.args, timeout)

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

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
    owner_record = (
        json.loads((owner / "owner.json").read_bytes())
        if (owner / "owner.json").exists()
        else None
    )
    failure_record = json.loads((owner / "failure.json").read_bytes())
    if failure_stage == "protocol":
        assert owner_record is not None
        assert owner_record["state"] == "process_running"
    else:
        assert owner_record is None
    assert failure_record["code"] == "startup_failed"
    assert failure_record["server_pid"] == child.pid
    assert failure_record["owner_nonce"] == OWNER_NONCE
    assert failure_record["timestamp"].endswith("Z")
    evidence = (owner / "failure.json").read_text()
    if owner_record is not None:
        evidence += (owner / "owner.json").read_text()
    assert "repository-secret" not in evidence
    assert str(tmp_path) not in evidence
    if os.name == "posix":
        assert stat.S_IMODE((owner / "failure.json").stat().st_mode) == 0o600
    _assert_lsp_acl_is_owner_only(owner, inherited=False)
    _assert_lsp_acl_is_owner_only(owner / "cancellation", inherited=os.name == "nt")
    if owner_record is not None:
        _assert_lsp_acl_is_owner_only(owner / "owner.json", inherited=os.name == "nt")
    _assert_lsp_acl_is_owner_only(owner / "failure.json", inherited=os.name == "nt")


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
    with pytest.raises(ValueError, match="parent"):
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
        def close(self) -> None:
            raise AssertionError("pipe close must not run while child is alive")

    class PermanentlyAliveProcess:
        args = _command("--ignored-secret", "secret")
        pid = 525252
        stdin = BlockingClose()
        stdout = BlockingClose()
        stderr = BlockingClose()

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(self.args, timeout)

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

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
    with pytest.raises(lsp_process.StartupCleanupError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    elapsed = time.monotonic() - started
    assert elapsed < lsp_process._STARTUP_WAIT_SECONDS + 0.75
    assert (owner / "failure.json").is_file()
    assert (owner / "owner.json").is_file()


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
    assert children and all(child.poll() is not None for child in children)
    owner_nonces = {f"{index + 1:032x}" for index in range(5)}
    assert not any(
        any(nonce in thread.name for nonce in owner_nonces)
        for thread in threading.enumerate()
        if thread.name.startswith("lsp-")
    )


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
    initial_threads = {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    }
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
    assert {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    } == initial_threads


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

    assert [command for command, _kwargs in commands] == [
        [
            "icacls",
            str(owner),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(OI)(CI)(F)",
        ],
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
    initial_threads = {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    }

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
        if isinstance(raised.value, lsp_process.StartupCleanupError):
            assert isinstance(raised.value.__cause__, RuntimeError)
            assert str(raised.value.__cause__) == "protocol startup failed"
        else:
            assert str(raised.value) == "protocol startup failed"
        return time.monotonic() - started, owner

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fail_start, range(20)))

    assert max(elapsed for elapsed, _owner in results) <= 2.75
    assert len(children) == 20
    assert all(child.poll() is not None for child in children)
    for _elapsed, owner in results:
        evidence = (owner / "failure.json").read_bytes()
        record = json.loads(evidence)
        assert record["code"] == "startup_failed"
        assert str(tmp_path).encode() not in evidence
        assert b"sleep-seconds" not in evidence
        _assert_lsp_acl_is_owner_only(owner / "failure.json", inherited=True)

    settle_deadline = time.monotonic() + 2
    while time.monotonic() < settle_deadline:
        current = {
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("lsp-")
        }
        if current == initial_threads:
            break
        time.sleep(0.01)
    assert current == initial_threads


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL subprocess contract")
def test_four_delayed_owner_acl_starts_share_deadline_and_leave_no_leaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initial_threads = {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    }

    def delayed_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        timeout = float(kwargs["timeout"])
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    def fail_start(index: int) -> tuple[float, Path]:
        owner = tmp_path / f"{index + 200:032x}"
        started = time.monotonic()
        with pytest.raises((PermissionError, lsp_process.StartupCleanupError)):
            LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
        return time.monotonic() - started, owner

    monkeypatch.setattr(subprocess, "run", delayed_run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fail_start, range(4)))

    assert max(elapsed for elapsed, _owner in results) <= 2.75
    assert all(owner.is_dir() and not any(owner.iterdir()) for _elapsed, owner in results)
    for _elapsed, owner in results:
        owner.rmdir()
    assert {
        thread.name for thread in threading.enumerate() if thread.name.startswith("lsp-")
    } == initial_threads


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


def test_protocol_close_fault_still_releases_tree_and_cleanup_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None
    protocol = generation.protocol
    assert protocol is not None
    close = protocol.close
    monkeypatch.setattr(
        protocol,
        "close",
        lambda _deadline: (_ for _ in ()).throw(OSError("protocol close failed")),
    )

    with pytest.raises(OSError, match="protocol close failed"):
        process.shutdown(time.monotonic() + 5)

    assert generation.tree is None
    assert generation.protocol is protocol
    assert coordinator.phase is lsp_process._LifecyclePhase.CLEANUP_PENDING
    assert (process.owner_root / "lease.json").is_file()

    monkeypatch.setattr(protocol, "close", close)
    process.shutdown(time.monotonic() + 5)
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
    assert events == ["protocol-stop", "terminate", "poll", "tree-close"]
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


def test_tree_cleanup_fault_retains_tree_owner_lease_and_scratch_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.tree is not None
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


def test_heartbeat_failure_during_cleanup_pending_is_stale_and_observable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--ignore-shutdown")
    coordinator = process._coordinator
    generation = coordinator.active
    assert generation is not None and generation.tree is not None
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
    assert isinstance(coordinator.background_cleanup_error, OSError)
    assert process.state is ProcessState.FAILED

    tree_fault = False
    with pytest.raises(OSError, match="cleanup-pending heartbeat failed"):
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

    def fail_heartbeat(current: object, record: object) -> None:
        if current is owner:
            attempted.set()
            raise OSError("heartbeat write failed")
        real_write_lease(current, record)

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

    def fail_heartbeat(current: object, _record: object) -> None:
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
        assert process.state is ProcessState.FAILED
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
            assert release.wait(3)

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
        assert attempted.wait(1)
        heartbeat.join(0.25)
        assert not heartbeat.is_alive()
        assert coordinator.pending_failure_intents >= 1
    finally:
        release.set()
        holder.join(1)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process._LifecyclePhase.RUNNING,
    )
    with pytest.raises(TimeoutError, match="drain inspection"):
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
        assert isinstance(coordinator.background_cleanup_error, TimeoutError)
    finally:
        release.set()
        holder.join(1)
        inspector.join(1)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process._LifecyclePhase.RUNNING,
    )
    with pytest.raises(TimeoutError, match="drain inspection"):
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
    assert coordinator.cleanup_result.ownership_pending is True
    monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", stop_io)

    with pytest.raises(OSError, match="background protocol cleanup failed"):
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
    restart_errors: list[BaseException] = []
    real_write_lease = lsp_process._OwnerDirectory.write_lease

    def delayed_heartbeat(current: object, record: object) -> None:
        assert isinstance(record, dict)
        if (
            current is owner
            and threading.current_thread() is coordinator.heartbeat_thread
            and record["generation_nonce"] == first_generation
        ):
            heartbeat_writing.set()
            assert release_heartbeat.wait(3)
        real_write_lease(current, record)  # type: ignore[arg-type]

    def restart() -> None:
        try:
            process.restart(time.monotonic() + 5)
        except BaseException as error:
            restart_errors.append(error)
        finally:
            restart_finished.set()

    monkeypatch.setattr(
        lsp_process._OwnerDirectory,
        "write_lease",
        delayed_heartbeat,
    )
    coordinator.heartbeat_wake.set()
    assert heartbeat_writing.wait(2)
    restart_thread = threading.Thread(target=restart)
    restart_thread.start()
    restart_returned_before_heartbeat = restart_finished.wait(0.2)
    release_heartbeat.set()
    restart_thread.join(5)

    assert restart_returned_before_heartbeat is False
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
    assert (process.owner_root / "lease.json").is_file()

    fault_enabled = False
    process.close(time.monotonic() + 5)
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
    if mutation == "wrong-owner":
        record["owner_nonce"] = "b" * 32
    elif mutation == "wrong-generation":
        record["generation_nonce"] = "b" * 32
    elif mutation == "wrong-code":
        record["code"] = "process_exited"
    elif mutation == "missing-pid":
        record.pop("server_pid")
    elif mutation == "wrong-pid":
        record["server_pid"] = process.process.pid + 1
    elif mutation == "noncanonical-timestamp":
        record["timestamp"] = "2026-07-24T12:34:56.1Z"
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)
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
        assert isinstance(coordinator.background_cleanup_error, ValueError)
        assert coordinator.cleanup_result.evidence == "failed"
        assert coordinator.cleanup_result.ownership_pending is True
        assert coordinator.owner_directory is not None
        assert (process.owner_root / "lease.json").is_file()

        failure.unlink()
        with pytest.raises(ValueError, match="failure evidence"):
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


def _invalid_request_params(case: str) -> object:
    if case == "cycle":
        value: list[object] = []
        value.append(value)
        return value
    if case == "nan":
        return {"value": float("nan")}
    if case == "inf":
        return {"value": float("inf")}
    if case == "surrogate":
        return {"value": "\ud800"}
    if case == "object":
        return {"value": object()}
    if case == "oversize":
        return {"value": "x" * MAX_FRAME_BYTES}
    raise AssertionError(case)


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
    acquired = threading.Event()
    release = threading.Event()
    before = (
        process.process,
        process.protocol,
        process.generation_nonce,
        process.restart_count,
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
        ) == before
        assert coordinator.candidate is None
    finally:
        release.set()
        holder.join(1)
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
        try:
            returned.append(_start(tmp_path, "--lifecycle", "--sleep-seconds", "30"))
        except BaseException as raised:
            error = raised
    finally:
        for process in returned:
            process.close(time.monotonic() + 5)
        if isinstance(error, lsp_process.StartupCleanupError):
            coordinator = error.coordinator
            assert coordinator is not None
            lsp_process._drive_cleanup(
                None,
                time.monotonic() + 5,
                terminal=True,
                failure_code="startup_failed",
                coordinator_override=coordinator,
            )

    assert time.monotonic() - started >= lsp_process._STARTUP_WAIT_SECONDS
    assert returned == []
    assert error is not None
    causes: list[BaseException] = []
    current: BaseException | None = error
    while current is not None:
        causes.append(current)
        current = current.__cause__
    assert any(isinstance(item, TimeoutError) for item in causes)
    settle_deadline = time.monotonic() + 2
    while any(child.poll() is None for child in children) and time.monotonic() < settle_deadline:
        time.sleep(0.01)
    assert children and all(child.poll() is not None for child in children)


def test_restart_nonce_failure_after_retirement_is_terminal_and_sticky(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    generation_nonce = process.generation_nonce
    server_pid = process.process.pid
    failure_path = process.owner_root / "failure.json"
    monkeypatch.setattr(
        lsp_process,
        "_new_generation_nonce",
        lambda: (_ for _ in ()).throw(RuntimeError("restart nonce failed")),
    )

    try:
        with pytest.raises(RuntimeError, match="restart nonce failed"):
            process.restart(time.monotonic() + 5)

        assert process.state is ProcessState.FAILED
        failure = json.loads(failure_path.read_bytes())
        assert failure["code"] == "restart_failed"
        assert failure["generation_nonce"] == generation_nonce
        assert failure["server_pid"] == server_pid
        process.close(time.monotonic() + 5)
        assert json.loads(failure_path.read_bytes()) == failure
        assert process.owner_root.is_dir()
    finally:
        if lsp_process._coordinator_has_ownership(process._coordinator):
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
    wait_deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < wait_deadline:
        time.sleep(0.01)
    descendant = int(pid_file.read_text(encoding="ascii"))
    coordinator = process._coordinator
    lsp_process._stop_recovery_owner(coordinator, time.monotonic() + 1)
    generation = coordinator.active
    assert generation is not None and generation.tree is not None
    tree = generation.tree
    terminate = lsp_process.ProcessTree.terminate
    entered = threading.Event()
    release = threading.Event()
    cleanup_errors: list[BaseException] = []
    heartbeat_records: list[dict[str, object]] = []
    lease_path = process.owner_root / "lease.json"
    initial = json.loads(lease_path.read_bytes())
    write_lease = lsp_process._OwnerDirectory.write_lease

    def observe_heartbeat(current: object, record: object) -> None:
        assert isinstance(record, dict)
        if threading.current_thread() is coordinator.heartbeat_thread:
            heartbeat_records.append(dict(record))
        write_lease(current, record)  # type: ignore[arg-type]

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
    heartbeat_deadline = time.monotonic() + 0.3
    while len(heartbeat_records) < 2 and time.monotonic() < heartbeat_deadline:
        time.sleep(0.01)
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
    monkeypatch.setattr(workspace, "delete_handle", lambda _handle: None)
    monkeypatch.setattr(workspace, "close_handle", close_handle)
    return owner


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

    def fail_heartbeat(current: object, record: object) -> None:
        if current is owner and threading.current_thread() is heartbeat:
            attempted.set()
            raise OSError("later heartbeat failed")
        real_write_lease(current, record)  # type: ignore[arg-type]

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
