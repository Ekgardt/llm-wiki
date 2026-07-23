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
import pytest
from lsp_process import (
    LSP_ENV_ALLOWLIST,
    MAX_STDERR_BYTES,
    LspProcess,
    ProcessState,
    lsp_environment,
)
from lsp_protocol import (
    CancellationSource,
    JsonRpcResponseError,
    ProtocolViolation,
    RequestCancelled,
)

FAKE_SERVER = Path(__file__).with_name("fake_lsp_server.py").resolve()
OWNER_NONCE = "a" * 32


def _command(*arguments: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *arguments]


def _start(tmp_path: Path, *arguments: str) -> LspProcess:
    return LspProcess.start(
        _command(*arguments), cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE
    )


def _wait(process: LspProcess, seconds: float = 10) -> int:
    return process.wait_for_exit(time.monotonic() + seconds)


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
    public = {field.name for field in dataclasses.fields(LspProcess) if not field.name.startswith("_")}
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
    assert not _pid_alive(descendant)
    assert not process.owner_root.exists()
    assert all(thread is None or not thread.is_alive() for thread in owner_threads)


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
    assert all(not _pid_alive(pid) for pid in pids)


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


def test_expired_drain_forces_fresh_generation_without_retrying_deadline(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "hung"
    process = _start(tmp_path, "--lifecycle", "--hang-once-marker", str(marker))
    first_generation = process.generation_nonce

    with pytest.raises(TimeoutError):
        process.request("slow", {}, deadline=time.monotonic() + 0.05)
    assert process.restart_count == 0
    time.sleep(2.05)

    assert process.request("echo", {"fresh": True}, deadline=time.monotonic() + 5) == {
        "fresh": True
    }
    assert process.restart_count == 1
    assert process.generation_nonce != first_generation
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
    process._closing = True
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
    _wait(process, 15)

    expected = bytes(index % 251 for index in range(total - MAX_STDERR_BYTES, total))
    assert process.stderr_bytes() == expected
    assert len(process.stderr_bytes()) == MAX_STDERR_BYTES


def test_zero_stderr_and_concurrent_snapshots_never_block(tmp_path: Path) -> None:
    process = _start(tmp_path, "--sleep-seconds", "2")
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


def test_each_start_has_independent_lowercase_hex_nonces(tmp_path: Path) -> None:
    first = LspProcess.start(
        _command("--sleep-seconds", "2"),
        cwd=tmp_path,
        owner_root=tmp_path / ("a" * 32),
    )
    second = LspProcess.start(
        _command("--sleep-seconds", "2"),
        cwd=tmp_path,
        owner_root=tmp_path / ("b" * 32),
    )
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
    _wait(process)


def test_wait_for_exit_timeout_does_not_kill_process(tmp_path: Path) -> None:
    process = _start(tmp_path, "--exit-while-pending")
    with pytest.raises(TimeoutError):
        process.wait_for_exit(time.monotonic() - 1)
    assert process.process.poll() is None
    process.process.stdin.close()
    _wait(process)


def test_request_delegates_token_and_updates_last_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--echo")
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
    _wait(process)


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


def test_restart_thread_start_failure_cleans_new_tree_and_becomes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _start(tmp_path, "--lifecycle")
    spawned: list[object] = []
    real_spawn = lsp_process.ProcessTree.spawn
    real_start = threading.Thread.start

    def spawn(*args: object, **kwargs: object) -> object:
        tree = real_spawn(*args, **kwargs)
        spawned.append(tree)
        return tree

    def fail_restart_stderr(thread: threading.Thread) -> None:
        if thread.name.startswith("lsp-stderr-"):
            raise RuntimeError("restart stderr thread start failed")
        real_start(thread)

    monkeypatch.setattr(lsp_process.ProcessTree, "spawn", spawn)
    monkeypatch.setattr(threading.Thread, "start", fail_restart_stderr)

    with pytest.raises(RuntimeError, match="restart stderr thread start failed"):
        process.restart(time.monotonic() + 5)

    assert len(spawned) == 1
    assert spawned[0].process.poll() is not None
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
        "spawn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("restart spawn failed")),
    )

    with pytest.raises(OSError, match="restart spawn failed"):
        process.restart(time.monotonic() + 5)

    assert process.restart_count == 1
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


def test_heartbeat_stop_reports_deadline_with_live_owner_thread(
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")

    with pytest.raises(TimeoutError, match="heartbeat"):
        process._stop_heartbeat(time.monotonic() - 1)

    process.close(time.monotonic() + 5)


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
        "spawn",
        lambda *_args, **_kwargs: _FakeTree(child),
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
    with pytest.raises(lsp_process.StartupCleanupError, match="direct child remains alive") as raised:
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
        "spawn",
        lambda *_args, **_kwargs: _FakeTree(child),
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

        def race_publish(parent: int, name: str) -> int:
            if name == "owner.json":
                handle = real_create(parent, name)
                try:
                    lsp_process._windows_workspace.write_all(
                        handle, attacker, chunk_bytes=4096
                    )
                    lsp_process._windows_workspace.flush_file(handle)
                finally:
                    lsp_process._windows_workspace.close_handle(handle)
            return real_create(parent, name)

        monkeypatch.setattr(lsp_process._windows_workspace, "create_file", race_publish)
    else:
        real_open = os.open

        def race_publish(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == "owner.json" and flags & os.O_EXCL:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                try:
                    os.write(descriptor, attacker)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(lsp_process.os, "open", race_publish)
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
    attacker = b"preexisting-failure-evidence"
    failure_path = process.owner_root / "failure.json"
    failure_path.write_bytes(attacker)
    process.process.stdin.close()
    _wait(process)
    assert process.state is ProcessState.DEGRADED
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
        process._closing = True
        process.process.stdin.close()
        _wait(process)
        assert replacement is None
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
        process._closing = True
        process.process.stdin.close()
        _wait(process)
        assert replacement is None
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

    assert json.loads((moved / "failure.json").read_bytes())["code"] == "process_exited"
    assert sentinel.read_bytes() == b"replacement"
    assert not (owner / "failure.json").exists()


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
        process._closing = True
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

    with pytest.raises(RuntimeError, match="exited during startup"):
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
        real_open = os.open

        def create_after_swap(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replacement
            if path == "owner.json" and flags & os.O_EXCL:
                owner.rename(moved)
                owner.mkdir()
                sentinel = owner / "sentinel.txt"
                owner_record = owner / "owner.json"
                failure_record = owner / "failure.json"
                sentinel.write_bytes(b"replacement-sentinel")
                owner_record.write_bytes(b"replacement-owner")
                failure_record.write_bytes(b"replacement-failure")
                replacement = sentinel, owner_record, failure_record
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(lsp_process.os, "open", create_after_swap)

    if os.name == "nt":
        process = _start(tmp_path, "--exit-while-pending")
        process.process.stdin.close()
        _wait(process)
        assert replacement is None
        assert json.loads((owner / "owner.json").read_bytes())["state"] == "process_running"
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


def test_rollback_writes_evidence_before_waits_and_never_extends_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, float]] = []
    clock = [10.0]

    class Process:
        pid = 515151
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        dead = False
        waits = 0

        def poll(self) -> int | None:
            return 0 if self.dead else None

        def terminate(self) -> None:
            events.append(("terminate", clock[0]))

        def kill(self) -> None:
            events.append(("kill", clock[0]))

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            events.append(("wait", clock[0]))
            clock[0] += timeout
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            self.dead = True
            return 0

    class Protocol:
        def _stop_io_for_process_cleanup(self) -> None:
            events.append(("stop", clock[0]))

        def _finish_io_after_process_exit(self, deadline: float) -> None:
            events.append(("finish", deadline))

    class Owner:
        owner_handle = 1
        owner_permissions_verified = True

        def close(self) -> None:
            events.append(("close-owner", clock[0]))

    def evidence(*_args: object, **_kwargs: object) -> None:
        events.append(("evidence", clock[0]))

    monkeypatch.setattr(lsp_process.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lsp_process, "_write_failure_record", evidence)

    lsp_process._rollback_startup(
        Process(),
        Protocol(),
        (),
        Owner(),
        deadline=12.0,
        owner_nonce=OWNER_NONCE,
        generation_nonce="b" * 32,
    )

    names = [name for name, _when in events]
    assert names.index("evidence") < names.index("wait")
    assert "finish" not in names
    assert clock[0] == 12.0


def test_rollback_skips_evidence_and_optional_cleanup_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter([23.0])
    evidence_called = False
    protocol_called = False

    class Protocol:
        def _stop_io_for_process_cleanup(self) -> None:
            nonlocal protocol_called
            protocol_called = True

    class Owner:
        owner_handle = 1
        owner_permissions_verified = True

        def close(self) -> None:
            return None

    def evidence(*_args: object, **_kwargs: object) -> None:
        nonlocal evidence_called
        evidence_called = True

    monkeypatch.setattr(
        lsp_process.time, "monotonic", lambda: next(moments, 23.0)
    )
    monkeypatch.setattr(lsp_process, "_write_failure_record", evidence)

    lsp_process._rollback_startup(
        None,
        Protocol(),
        (),
        Owner(),
        deadline=22.0,
        owner_nonce=OWNER_NONCE,
        generation_nonce="b" * 32,
    )

    assert evidence_called is False
    assert protocol_called is True


def test_partial_thread_join_retry_sleep_never_exceeds_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [30.0]

    class StartingThread:
        def join(self, timeout: float) -> None:
            clock[0] += min(0.0004, timeout)
            raise RuntimeError("thread is still starting")

    thread = StartingThread()
    monkeypatch.setattr(lsp_process.threading, "enumerate", lambda: [thread])
    monkeypatch.setattr(lsp_process.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lsp_process.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration)
    )

    lsp_process._join_partially_started_thread(thread, 30.0005)

    assert clock[0] <= 30.0005


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
