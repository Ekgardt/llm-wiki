"""An LSP child gets an allowlisted environment, bounded stderr and exact owner records."""

from __future__ import annotations

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
import pytest
from llm_wiki import (
    lsp_process_state,
)
from lsp_process import (
    LSP_ENV_ALLOWLIST,
    MAX_STDERR_BYTES,
    LspProcess,
    ProcessState,
    lsp_environment,
)
from lsp_protocol import (
    CancellationSource,
    ProtocolViolation,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _STARTUP_BUDGET_SECONDS,
    OWNER_NONCE,
    _assert_lsp_acl_is_owner_only,
    _command,
    _coordinator_wait,
    _expect_active_generation_exit,
    _start,
    _wait,
)
from tests.slow_machine import SHORT_TIMEOUT

_OS_INJECTED_ENVIRONMENT = frozenset({"__CF_USER_TEXT_ENCODING"})


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
    environment = process.request("environment", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
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

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", spy)
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
        thread.join(SHORT_TIMEOUT)
        assert not thread.is_alive()
    assert process.stderr_bytes() == b""
    process.close(time.monotonic() + 5)


# The stub counts this from its own start, so it has to outlive the parent's
# startup, not just the assertion. At two seconds it did not: on 2026-08-22 the
# hosted Windows shard of CI run 32542795352 (job 96955875519) recorded
# `returncode: 0` with `started_monotonic=730.375` — the stub had slept its two
# seconds out and exited cleanly while startup was still running, and the
# coordinator correctly called that a terminal failure. The file already answers
# the same problem for stderr with a three-second linger; this is the same
# answer with more room. The price is that each parametrization waits this long
# for the exit it asserts.
_SELF_EXIT_SECONDS = 8.0
_SELF_EXIT_WAIT_SECONDS = 20.0


def _expect_protocol_violations(futures) -> None:
    for future in futures:
        with pytest.raises(ProtocolViolation):
            future.result(timeout=5)


def _join_recovery_threads(processes: tuple[LspProcess, ...]) -> None:
    for process in processes:
        recovery = process._recovery_thread
        if recovery is not None:
            recovery.join(SHORT_TIMEOUT)


def _assert_lowercase_hex_nonce(nonce: str) -> None:
    assert len(nonce) == 32
    assert nonce == nonce.lower()
    int(nonce, 16)


def test_each_start_has_independent_lowercase_hex_nonces(tmp_path: Path) -> None:
    # CI run 33034799091 (Windows py3.10-s1) hit the defect the constant above
    # records: at a bare two-second sleep the stub exited while the second
    # startup was still running on the loaded runner, the coordinator correctly
    # raised "LSP terminal failure was recorded during startup", and teardown
    # then reported the never-closed start's leftover nonce.
    first = LspProcess.start(
        _command("--sleep-seconds", str(_SELF_EXIT_SECONDS)),
        cwd=tmp_path,
        owner_root=tmp_path / ("a" * 32),
    )
    _expect_active_generation_exit(first)
    second = LspProcess.start(
        _command("--sleep-seconds", str(_SELF_EXIT_SECONDS)),
        cwd=tmp_path,
        owner_root=tmp_path / ("b" * 32),
    )
    _expect_active_generation_exit(second)
    _wait(first, _SELF_EXIT_WAIT_SECONDS)
    _wait(second, _SELF_EXIT_WAIT_SECONDS)
    _join_recovery_threads((first, second))
    for nonce in (
        first.owner_nonce,
        first.generation_nonce,
        second.owner_nonce,
        second.generation_nonce,
    ):
        _assert_lowercase_hex_nonce(nonce)
    assert (first.owner_nonce, second.owner_nonce) == ("a" * 32, "b" * 32)
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
        "owner_start_identity",
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
        compile_cache.verify_owner_only(process.owner_root, 0o700)
        compile_cache.verify_owner_only(process.owner_root / "cancellation", 0o700)
        compile_cache.verify_owner_only(owner_file, 0o600)
        compile_cache.verify_owner_only(process.owner_root / "lease.json", 0o600)
        assert stat.S_IMODE(process.owner_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((process.owner_root / "cancellation").stat().st_mode) == 0o700
        assert stat.S_IMODE(owner_file.stat().st_mode) == 0o600
    process.close(time.monotonic() + 5)


@pytest.mark.parametrize("deadline", [math.nan, math.inf, "later", True])
def test_wait_for_exit_rejects_invalid_deadline(tmp_path: Path, deadline: object) -> None:
    process = _start(tmp_path, "--sleep-seconds", str(_SELF_EXIT_SECONDS))
    with pytest.raises((TypeError, ValueError)):
        process.wait_for_exit(deadline)  # type: ignore[arg-type]
    _expect_active_generation_exit(process)
    _wait(process, _SELF_EXIT_WAIT_SECONDS)
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
                deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            )
            for _ in range(2)
        ]
        _expect_protocol_violations(futures)

    _wait(process)
    assert _coordinator_wait(
        process,
        lambda: process._coordinator.phase
        is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
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
        process.request("later", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
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
