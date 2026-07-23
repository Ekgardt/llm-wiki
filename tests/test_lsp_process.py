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

import lsp_process
import pytest
from lsp_process import (
    LSP_ENV_ALLOWLIST,
    MAX_STDERR_BYTES,
    LspProcess,
    ProcessState,
    lsp_environment,
)
from lsp_protocol import CancellationSource, ProtocolViolation

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
    }


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
    environment = process.request("environment", {}, deadline=time.monotonic() + 5)
    assert isinstance(environment, dict)
    assert set(environment) <= LSP_ENV_ALLOWLIST
    assert not any(
        name.startswith("PYTHON") or name in {"NODE_OPTIONS", "OPENAI_API_KEY"}
        for name in environment
    )
    _wait(process)


def test_popen_uses_exact_safe_binary_pipe_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    real_popen = subprocess.Popen

    def spy(args: object, **kwargs: object):
        calls.append((args, kwargs))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(lsp_process.subprocess, "Popen", spy)
    process = _start(tmp_path)
    _wait(process)

    assert len(calls) == 1
    arguments, options = calls[0]
    assert isinstance(arguments, list)
    assert Path(arguments[0]).is_absolute()
    assert options == {
        "cwd": tmp_path.resolve(),
        "env": lsp_environment(),
        "shell": False,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }


def test_stderr_ring_retains_exact_last_four_mib_across_odd_chunks(tmp_path: Path) -> None:
    total = 5 * 1024 * 1024 + 17
    process = _start(tmp_path, "--stderr-bytes", str(total))
    _wait(process, 15)

    expected = bytes(index % 251 for index in range(total - MAX_STDERR_BYTES, total))
    assert process.stderr_bytes() == expected
    assert len(process.stderr_bytes()) == MAX_STDERR_BYTES


def test_zero_stderr_and_concurrent_snapshots_never_block(tmp_path: Path) -> None:
    process = _start(tmp_path)
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
        _command(), cwd=tmp_path, owner_root=tmp_path / ("a" * 32)
    )
    second = LspProcess.start(
        _command(), cwd=tmp_path, owner_root=tmp_path / ("b" * 32)
    )
    _wait(first)
    _wait(second)
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


def test_owner_json_is_canonical_redacted_restricted_and_has_only_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_argument = str(tmp_path / "repository-secret-path" / "credential-value")
    environment_secret = "allowlisted-environment-secret-7f31"
    monkeypatch.setenv("TMP", environment_secret)
    process = _start(tmp_path, "--ignored-secret", secret_argument)
    _wait(process)
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
    assert record["command_basename"] == Path(sys.executable).name
    assert record["state"] == "process_running"
    persisted = raw.decode()
    assert secret_argument not in persisted
    assert environment_secret not in persisted
    assert str(tmp_path.resolve()) not in persisted
    assert str(Path(sys.executable).resolve()) not in persisted
    assert set(path.name for path in process.owner_root.iterdir()) == {
        "cancellation",
        "failure.json",
        "owner.json",
    }
    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert set(failure) == {
        "code",
        "generation_nonce",
        "owner_nonce",
        "owner_pid",
        "timestamp",
    }
    assert failure["code"] == "process_exited"
    assert failure["owner_pid"] == process.process.pid
    assert failure["owner_nonce"] == process.owner_nonce
    assert failure["generation_nonce"] == process.generation_nonce
    assert failure["timestamp"].endswith("Z")
    lsp_process._verify_owner_only(process.owner_root, 0o700)
    lsp_process._verify_owner_only(process.owner_root / "cancellation", 0o700)
    lsp_process._verify_owner_only(owner_file, 0o600)
    lsp_process._verify_owner_only(process.owner_root / "failure.json", 0o600)
    if os.name == "posix":
        assert stat.S_IMODE(process.owner_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((process.owner_root / "cancellation").stat().st_mode) == 0o700
        assert stat.S_IMODE(owner_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("deadline", [math.nan, math.inf, "later", True])
def test_wait_for_exit_rejects_invalid_deadline(tmp_path: Path, deadline: object) -> None:
    process = _start(tmp_path)
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
    assert "owner_pid" in failure


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
    process = _start(tmp_path)
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
    real_mkdir = Path.mkdir

    def fail_cancellation(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "cancellation":
            raise OSError("cancellation directory failed")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_cancellation)
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
    monkeypatch.setattr(lsp_process.subprocess, "Popen", lambda *_args, **_kwargs: child)
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
    assert failure_record["owner_pid"] == child.pid
    assert failure_record["owner_nonce"] == OWNER_NONCE
    assert failure_record["timestamp"].endswith("Z")
    evidence = (owner / "failure.json").read_text()
    if owner_record is not None:
        evidence += (owner / "owner.json").read_text()
    assert "repository-secret" not in evidence
    assert str(tmp_path) not in evidence
    if os.name == "posix":
        assert stat.S_IMODE((owner / "failure.json").stat().st_mode) == 0o600
    lsp_process._verify_owner_only(owner, 0o700)
    lsp_process._verify_owner_only(owner / "cancellation", 0o700)
    if owner_record is not None:
        lsp_process._verify_owner_only(owner / "owner.json", 0o600)
    lsp_process._verify_owner_only(owner / "failure.json", 0o600)


def test_owner_permission_failure_rolls_back_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned = False

    def fail_permissions(_path: Path, _mode: int) -> None:
        raise PermissionError("ACL unavailable")

    def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not run")

    monkeypatch.setattr(lsp_process, "_restrict_owner_only", fail_permissions)
    monkeypatch.setattr(lsp_process.subprocess, "Popen", unexpected_spawn)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(lsp_process.StartupCleanupError) as raised:
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert isinstance(raised.value.__cause__, PermissionError)
    assert spawned is False
    assert owner.is_dir()


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


def test_parent_identity_change_after_owner_creation_rolls_back_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities = iter([(1, 2), (1, 3)])
    spawned = False

    monkeypatch.setattr(lsp_process, "_parent_identity", lambda _path: next(identities))

    def unexpected_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not run")

    monkeypatch.setattr(lsp_process.subprocess, "Popen", unexpected_spawn)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(lsp_process.StartupCleanupError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert spawned is False
    assert owner.is_dir()


def test_parent_identity_change_after_spawn_terminates_child_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stable = (1, 2)
    identities = iter([stable, stable, stable, stable, (1, 3)])
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    monkeypatch.setattr(lsp_process, "_parent_identity", lambda _path: next(identities))

    def popen_spy(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(lsp_process.subprocess, "Popen", popen_spy)
    owner = tmp_path / OWNER_NONCE
    with pytest.raises(lsp_process.StartupCleanupError):
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
    real_info = parent.lstat()

    class ReparseInfo:
        st_mode = real_info.st_mode
        st_dev = real_info.st_dev
        st_ino = real_info.st_ino
        st_file_attributes = 0x400

    real_lstat = Path.lstat

    def reparse_lstat(path: Path):
        return ReparseInfo() if path == parent else real_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    owner = parent / OWNER_NONCE
    with pytest.raises(ValueError, match="reparse"):
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

    monkeypatch.setattr(lsp_process.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(threading.Thread, "start", skip_stderr_owner)
    monkeypatch.setattr(lsp_process, "_restrict_owner_only", lambda _path, _mode: None)
    monkeypatch.setattr(lsp_process, "_verify_owner_only", lambda _path, _mode: None)
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

    real_open = os.open

    def race_publish(path: object, flags: int, mode: int = 0o777) -> int:
        target = Path(path)
        if target.name == "owner.json" and flags & os.O_EXCL:
            target.write_bytes(attacker)
        return real_open(path, flags, mode)

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
    assert process.state is ProcessState.FAILED
    assert failure_path.read_bytes() == attacker
    assert json.loads((process.owner_root / "owner.json").read_bytes())["state"] == (
        "process_running"
    )


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
        replacement = _replace_owner_root_with_attacker(owner, moved)
        return result

    monkeypatch.setattr(lsp_process, "_write_owner_record", write_then_swap)
    monkeypatch.setattr(
        lsp_process.subprocess,
        "Popen",
        lambda *args, **kwargs: children.append(real_popen(*args, **kwargs)) or children[-1],
    )
    with pytest.raises(lsp_process.StartupCleanupError):
        LspProcess.start(_command(), cwd=tmp_path, owner_root=owner)
    assert replacement is not None
    sentinel, owner_record = replacement
    assert sentinel.read_bytes() == b"attacker-sentinel"
    assert owner_record.read_bytes() == b"attacker-owner"
    assert owner.is_dir()
    assert moved.is_dir()
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
            replacement = _replace_owner_root_with_attacker(owner, moved)

    monkeypatch.setattr(threading.Thread, "start", start_then_swap)
    monkeypatch.setattr(
        lsp_process.subprocess,
        "Popen",
        lambda *args, **kwargs: children.append(real_popen(*args, **kwargs)) or children[-1],
    )
    with pytest.raises(lsp_process.StartupCleanupError):
        LspProcess.start(
            _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
        )
    assert replacement is not None
    sentinel, owner_record = replacement
    assert sentinel.read_bytes() == b"attacker-sentinel"
    assert owner_record.read_bytes() == b"attacker-owner"
    assert owner.is_dir()
    assert moved.is_dir()
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
        replacement = _replace_owner_root_with_attacker(owner, moved)
        return real_evidence(*args, **kwargs)

    monkeypatch.setattr(lsp_process, "LspProtocol", BrokenProtocol)
    monkeypatch.setattr(lsp_process, "_write_failure_record", swap_before_evidence)
    with pytest.raises(lsp_process.StartupCleanupError):
        LspProcess.start(
            _command("--sleep-seconds", "30"), cwd=tmp_path, owner_root=owner
        )
    assert replacement is not None
    sentinel, owner_record = replacement
    assert sentinel.read_bytes() == b"attacker-sentinel"
    assert owner_record.read_bytes() == b"attacker-owner"
    assert owner.is_dir()
    assert moved.is_dir()
