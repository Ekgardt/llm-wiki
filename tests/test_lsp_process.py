"""Process ownership and bounded transport tests for LSP children: the public shape and the bootstrap and restart generations."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path

import lsp_protocol
import pytest
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_failure,
    lsp_process_generation,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_recovery,
    lsp_process_restart,
    lsp_process_state,
)
from lsp_process import (
    LSP_ENV_ALLOWLIST,
    MAX_STDERR_BYTES,
    LspProcess,
    ProcessState,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    )
from tests.lsp_process_scenario_helpers import (
    _BARRIER_SECONDS,
    _STARTUP_BUDGET_SECONDS,
    OWNER_NONCE,
    _command,
    _initialize_generation,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


def _expect_repeated_close_failure(process, message: str, attempts: int = 2) -> None:
    """The same failure must repeat until the blocked cleanup is released."""
    for _attempt in range(attempts):
        with pytest.raises(OSError, match=message):
            process.close(time.monotonic() + 5)


def _retry_cleanup_errors(errors) -> None:
    for error in errors:
        if isinstance(error, lsp_process_cleanup.StartupCleanupError):
            error.retry_cleanup(time.monotonic() + 5)


def _close_owned(processes) -> None:
    for process in processes:
        if lsp_process_state.coordinator_has_ownership(process._coordinator):
            process.close(time.monotonic() + 5)


def _reraise_unless_windows_retry(deadline: float) -> None:
    """Windows may deny a lease read while it is replaced; retry until deadline."""
    if os.name != "nt" or time.monotonic() >= deadline:
        raise
    time.sleep(0.01)


def _expected_windows_denials() -> int:
    return 1 if os.name == "nt" else 0


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
    monkeypatch.setattr(lsp_process_limits, "HEARTBEAT_SECONDS", 0.05)
    coordinators: list[lsp_process_state.LifecycleCoordinator] = []
    real_prepare = lsp_process_generation.prepare_generation
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    bootstrap_nonces: list[str] = []
    started: list[LspProcess] = []
    start_errors: list[BaseException] = []

    def observe_prepare(
        coordinator: lsp_process_state.LifecycleCoordinator,
        *args: object,
        **kwargs: object,
    ) -> lsp_process_state.Generation:
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
                    deadline=time.monotonic() + SHORT_TIMEOUT,
                    server_request_handlers={
                        "workspace/configuration": lambda _params: True
                    },
                    server_notification_handlers={"$/progress": lambda _params: None},
                    generation_bootstrap=bootstrap,
                )
            )
        except BaseException as error:
            start_errors.append(error)

    monkeypatch.setattr(lsp_process_generation, "prepare_generation", observe_prepare)
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
        assert bootstrap_entered.wait(LONG_TIMEOUT)
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
        thread.join(SHORT_TIMEOUT)
        assert not thread.is_alive()
        assert start_errors == []
        assert len(started) == 1
        assert owner_exists_during_bootstrap is True
        assert lease_exists_during_bootstrap is True
        assert transient_read_denials == _expected_windows_denials()
        assert heartbeat_refreshed is True
        assert lease_record["generation_nonce"] == bootstrap_nonces[0]
        assert transition_snapshot == (
            lsp_process_state.LifecyclePhase.STARTING,
            None,
            bootstrap_nonces[0],
            bootstrap_nonces[0],
            False,
        )
        assert started[0].state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        release_bootstrap.set()
        thread.join(SHORT_TIMEOUT)
        _close_owned(started)
        _retry_cleanup_errors(start_errors)


def test_startup_commit_rejects_heartbeat_terminal_failure_during_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process_limits, "HEARTBEAT_SECONDS", 60.0)
    coordinators: list[lsp_process_state.LifecycleCoordinator] = []
    real_prepare = lsp_process_generation.prepare_generation
    real_write_lease = lsp_process_owner.OwnerDirectory.write_lease
    real_select_terminal = lsp_process_failure.select_terminal_failure_locked
    bootstrap_entered = threading.Event()
    release_bootstrap = threading.Event()
    heartbeat_failed = threading.Event()
    terminal_recorded = threading.Event()
    write_lock = threading.Lock()
    lease_writes = 0
    started: list[LspProcess] = []
    start_errors: list[BaseException] = []

    def observe_prepare(
        coordinator: lsp_process_state.LifecycleCoordinator,
        *args: object,
        **kwargs: object,
    ) -> lsp_process_state.Generation:
        coordinators.append(coordinator)
        return real_prepare(coordinator, *args, **kwargs)  # type: ignore[arg-type]

    def fail_second_lease(
        owner: lsp_process_owner.OwnerDirectory,
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
        coordinator: lsp_process_state.LifecycleCoordinator,
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
                    deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
                    server_request_handlers={},
                    server_notification_handlers={},
                    generation_bootstrap=bootstrap,
                )
            )
        except BaseException as error:
            start_errors.append(error)

    monkeypatch.setattr(lsp_process_generation, "prepare_generation", observe_prepare)
    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", fail_second_lease)
    monkeypatch.setattr(
        lsp_process_failure,
        "select_terminal_failure_locked",
        observe_terminal,
    )
    monkeypatch.setattr(lsp_process_recovery, "start_lifecycle_workers", lambda *_args: None)
    owner_root = tmp_path / OWNER_NONCE
    thread = threading.Thread(target=start)
    thread.start()
    assert bootstrap_entered.wait(LONG_TIMEOUT)
    coordinator = coordinators[0]
    heartbeat = coordinator.heartbeat_thread
    assert heartbeat is not None
    coordinator.heartbeat_wake.set()
    assert heartbeat_failed.wait(_BARRIER_SECONDS)
    assert terminal_recorded.wait(_BARRIER_SECONDS)
    with coordinator.terminal_state_lock:
        assert coordinator.pending_failure_intents == 1
        assert coordinator.success_committed is False
    with coordinator.condition:
        assert coordinator.terminal_outcome == "failure"
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPING_FAILURE

    release_bootstrap.set()
    thread.join(SHORT_TIMEOUT)
    try:
        assert not thread.is_alive()
        assert started == []
        assert len(start_errors) == 1
        assert coordinator.startup_complete is False
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
        assert (owner_root / "failure.json").is_file()
        assert not (owner_root / "lease.json").exists()
    finally:
        release_bootstrap.set()
        thread.join(SHORT_TIMEOUT)
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
            "initialized/query", {"retry": True}, deadline=time.monotonic() + SHORT_TIMEOUT
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


BOOTSTRAP_BUDGET_SECONDS = 3.0


# The live deadline a replacement generation is aimed at. It is a floor, not
# the expected value: the wait that produces it can overshoot by a sizeable
# fraction of a second on the slowest supported machine, and the measured
# margin still has to stay clearly positive.
_REPLACEMENT_MARGIN_FLOOR_SECONDS = 1.0


def test_delayed_crash_restart_commits_with_a_short_live_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "delayed-query-crashed"
    bootstraps: list[tuple[str, float]] = []
    protocol_deadlines: list[float] = []
    replacement_margins: list[float] = []
    fresh_deadlines: list[float] = []
    commit_deadlines: list[float] = []
    real_protocol = lsp_process_generation.LspProtocol
    real_fresh_deadline = lsp_process_launch.fresh_bootstrap_deadline
    real_commit = lsp_process_restart.commit_restart_generation_owned

    def bootstrap(
        protocol: lsp_protocol.LspProtocol,
        pid: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        bootstraps.append((generation_nonce, deadline))
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    # How much live deadline the replacement is left with. Starting a child
    # interpreter costs far more on a loaded Windows runner than on a quiet
    # Linux one, so the target is derived from this machine's own measured
    # startup instead of a number that only holds on a fast host.
    #
    # The floor also has to absorb the wait below overshooting: on a loaded
    # macOS runner `Event().wait()` has been seen to return more than 150 ms
    # late, which is what the measured margin loses.
    target_margin = [_REPLACEMENT_MARGIN_FLOOR_SECONDS]

    def delayed_protocol(*args: object, **kwargs: object) -> lsp_protocol.LspProtocol:
        deadline = kwargs.get("_startup_deadline")
        assert isinstance(deadline, float)
        protocol_deadlines.append(deadline)
        if len(protocol_deadlines) == 2:
            delay = deadline - time.monotonic() - target_margin[0]
            if delay > 0:
                threading.Event().wait(delay)
            replacement_margins.append(deadline - time.monotonic())
        return real_protocol(*args, **kwargs)  # type: ignore[arg-type]

    def fresh_deadline(
        coordinator: lsp_process_state.LifecycleCoordinator,
    ) -> float:
        deadline = real_fresh_deadline(coordinator)
        fresh_deadlines.append(deadline)
        return deadline

    def commit(
        instance: LspProcess,
        candidate: lsp_process_state.Generation,
        generation_state: ProcessState,
        deadline: float,
    ) -> None:
        commit_deadlines.append(deadline)
        real_commit(instance, candidate, generation_state, deadline)

    monkeypatch.setattr(lsp_process_generation, "LspProtocol", delayed_protocol)
    monkeypatch.setattr(lsp_process_launch, "fresh_bootstrap_deadline", fresh_deadline)
    monkeypatch.setattr(lsp_process_restart, "commit_restart_generation_owned", commit)
    started = time.monotonic()
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
        bootstrap_timeout_seconds=BOOTSTRAP_BUDGET_SECONDS,
    )
    target_margin[0] = min(
        2.0,
        max(_REPLACEMENT_MARGIN_FLOOR_SECONDS, (time.monotonic() - started) * 2),
    )
    first_nonce = process.generation_nonce
    try:
        assert process.request(
            "initialized/query",
            {"delayed": True},
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        )["initialized"] is True

        assert process.restart_count == 1
        assert process.generation_nonce != first_nonce
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert len(fresh_deadlines) == 1
        replacement_deadline = fresh_deadlines[0]
        assert 0.25 <= replacement_margins[0] <= target_margin[0] + 0.1
        assert replacement_margins[0] < BOOTSTRAP_BUDGET_SECONDS
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=_initialize_generation,
    )
    coordinator = process._coordinator
    try:
        with coordinator.driver:
            assert lsp_process_state.enqueue_failure_intent(
                coordinator,
                lsp_process_state.FailureIntent(
                    process.generation_nonce,
                    "test terminal race",
                    False,
                    time.monotonic(),
                ),
            )

            assert process.promote_workspace_ready(
                generation_nonce=process.generation_nonce,
                deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            ) is False
            assert process.state is ProcessState.PROTOCOL_INITIALIZED

            coordinator.failure_queue.get_nowait()
            lsp_process_state.acknowledge_failure_intent(coordinator)
    finally:
        process.close(time.monotonic() + 5)
