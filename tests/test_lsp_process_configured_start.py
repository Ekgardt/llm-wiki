"""A configured LSP start installs its handlers and honours the caller's deadline."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lsp_protocol
import pytest
from llm_wiki import (
    lsp_process_generation,
    lsp_process_launch,
    lsp_process_owner,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)
from lsp_protocol import (
    JsonRpcResponseError,
    ProtocolViolation,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _STARTUP_BUDGET_SECONDS,
    OWNER_NONCE,
    _command,
    _coordinator_wait,
    _initialize_generation,
)
from tests.slow_machine import SHORT_TIMEOUT


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
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
    real_spawn = lsp_process_state.ProcessTree._spawn_with_deadline.__func__
    real_protocol = lsp_process_generation.LspProtocol
    real_create = lsp_process_owner.OwnerDirectory.create

    def record_owner_create(
        owner: lsp_process_owner.OwnerDirectory,
        deadline: float,
    ) -> None:
        deadlines["owner"] = deadline
        real_create(owner, deadline)

    def record_spawn(
        cls: type[lsp_process_state.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
    ) -> lsp_process_state.ProcessTree:
        deadlines["spawn"] = deadline
        return real_spawn(cls, command, cwd=cwd, env=env, deadline=deadline)  # type: ignore[arg-type]

    def record_protocol(*args: object, **kwargs: object) -> lsp_protocol.LspProtocol:
        deadlines["protocol"] = kwargs["_startup_deadline"]  # type: ignore[assignment]
        return real_protocol(*args, **kwargs)  # type: ignore[arg-type]

    def bootstrap(*args: object) -> ProcessState:
        deadlines["bootstrap"] = args[-1]  # type: ignore[assignment]
        return ProcessState.PROCESS_RUNNING

    monkeypatch.setattr(
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "create", record_owner_create)
    monkeypatch.setattr(lsp_process_generation, "LspProtocol", record_protocol)
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={},
        server_notification_handlers={},
        generation_bootstrap=lambda *_args: ProcessState.PROCESS_RUNNING,
        bootstrap_timeout_seconds=1.25,
    )
    try:
        configuration = process._coordinator.generation_configuration
        assert configuration.bootstrap_timeout_seconds == 1.25
        started = time.monotonic()
        restart_deadline = lsp_process_launch.fresh_bootstrap_deadline(
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

    monkeypatch.setattr(lsp_process_state.subprocess, "Popen", record_child)
    owner = tmp_path / OWNER_NONCE

    with pytest.raises(TimeoutError, match="generation bootstrap timed out"):
        LspProcess.start_configured(
            _command("--lifecycle"),
            cwd=tmp_path,
            owner_root=owner,
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    coordinator = process._coordinator

    with pytest.raises(
        ProtocolViolation,
        match="^LSP replacement startup failed$",
    ) as raised:
        process.request("initialized/query", {}, deadline=time.monotonic() + SHORT_TIMEOUT)

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "LSP replacement startup cause (RuntimeError)"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value.__cause__)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
            request_barrier.wait(timeout=SHORT_TIMEOUT)
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
                deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            )
        except BaseException as error:
            return error
        raise AssertionError("request unexpectedly survived failed replacement startup")

    monkeypatch.setattr(process.protocol, "request", synchronized_request)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(capture_failure, index) for index in range(2)]
            errors = [future.result(timeout=SHORT_TIMEOUT) for future in futures]
    finally:
        if lsp_process_state.coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)

    assert bootstraps == 2
    _assert_distinct_sanitized_failures(errors)
    causes = _assert_single_cause(errors)
    assert secret not in repr(errors)
    assert secret not in repr(causes)
