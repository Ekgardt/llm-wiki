"""An autonomous bootstrap and its callbacks follow one owned lifecycle."""

from __future__ import annotations

import inspect
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lsp_protocol
import pytest
from llm_wiki import (
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
    _close_owned_process,
    _command,
    _coordinator_wait,
    _FakeTree,
    _initialize_generation,
    _start,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


def _is_replacement_process(current: object, bootstraps) -> bool:
    """True for the second generation's tree, once it exists."""
    if len(bootstraps) <= 1:
        return False
    return current.process.pid == bootstraps[1][2]  # type: ignore[attr-defined]


def _await_release(release, deadline: float, message: str) -> None:
    if not release.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError(message)


def _hold_protocol_constructor_until_callback(
    monkeypatch: pytest.MonkeyPatch,
    callback_finished: threading.Event,
) -> None:
    protocol_type = lsp_process_generation.LspProtocol

    class ConstructorCallbackProbe(protocol_type):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            if not callback_finished.wait(2):
                raise TimeoutError("startup callback did not run before constructor return")

    monkeypatch.setattr(lsp_process_generation, "LspProtocol", ConstructorCallbackProbe)


def test_autonomous_bootstrap_uses_configured_budget_and_retains_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 0.15)
    monkeypatch.setattr(lsp_process_limits, "RECOVERY_RETRY_SECONDS", 0.02)
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

    # Two different numbers were one number here. The first start spawns a real
    # server and handshakes with it, so its deadline must be sized for the
    # slowest supported machine; the autonomous replacement budget is what this
    # test measures, and it must stay small because the replacement waits its
    # budget out. Sharing 0.8 s for both made a Windows runner fail the start
    # (CI 34725227244, 2026-09-12: "LSP startup deadline expired after initial
    # lease publication").
    configured_budget_seconds = 0.8
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
        bootstrap_timeout_seconds=configured_budget_seconds,
    )
    coordinator = process._coordinator
    configured_budget = coordinator.generation_configuration.bootstrap_timeout_seconds
    assert configured_budget is not None
    recovery = coordinator.recovery_thread
    heartbeat = coordinator.heartbeat_thread
    assert recovery is not None and heartbeat is not None
    terminate = lsp_process_state.ProcessTree.terminate
    write_failure_record = lsp_process_state.write_failure_record
    fresh_cleanup_deadline = lsp_process_launch.fresh_cleanup_deadline
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
        lsp_process_state.ProcessTree,
        "terminate",
        fail_first_replacement_cleanup,
    )
    monkeypatch.setattr(lsp_process_launch, "fresh_cleanup_deadline", record_failure_deadline)
    monkeypatch.setattr(lsp_process_state, "write_failure_record", consume_failure_deadline)
    try:
        process.protocol._become_fatal("trigger autonomous configured restart")
        assert replacement_bootstrap_started.wait(SHORT_TIMEOUT)
        assert first_cleanup_failed.wait(SHORT_TIMEOUT)

        replacement_started, replacement_deadline, _pid = bootstraps[1]
        assert 0 < replacement_deadline - replacement_started <= configured_budget
        assert cleanup_deadline_margins[0] >= 0.05
        assert cleanup_retry_started.wait(SHORT_TIMEOUT)
        assert coordinator.phase in {
            lsp_process_state.LifecyclePhase.CLEANUP_PENDING,
            lsp_process_state.LifecyclePhase.STOPPING_FAILURE,
        }
        assert coordinator.recovery_thread is recovery
        assert recovery.is_alive()
        assert heartbeat.is_alive()
        assert lsp_process_state.coordinator_has_ownership(coordinator)

        allow_cleanup_retry.set()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(SHORT_TIMEOUT)
        heartbeat.join(SHORT_TIMEOUT)
        assert not recovery.is_alive()
        assert not heartbeat.is_alive()
        assert coordinator.recovery_thread is None
        assert coordinator.heartbeat_thread is None
        assert coordinator.cleanup_result.ownership_pending is False
        assert not (process.owner_root / "lease.json").exists()
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        allow_cleanup_retry.set()
        _close_owned_process(process, coordinator)


def test_caller_restart_failure_keeps_deadline_and_retains_cleanup_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The point of the test is that the caller returns on its own deadline
    # instead of waiting out the graceful cleanup. Both numbers are sized for
    # the slowest supported machine and stay an order of magnitude apart, so a
    # caller that wrongly waits still fails the bound below.
    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 60.0)
    monkeypatch.setattr(lsp_process_limits, "RECOVERY_RETRY_SECONDS", 0.02)
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    coordinator = process._coordinator
    recovery = coordinator.recovery_thread
    write_failure_record = lsp_process_state.write_failure_record
    terminate = lsp_process_state.ProcessTree.terminate
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
    ) -> tuple[lsp_process_state.Generation, ProcessState]:
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
        lsp_process_restart,
        "prepare_restart_generation_owned",
        fail_restart_prepare,
    )
    monkeypatch.setattr(lsp_process_state, "write_failure_record", delay_failure_evidence)
    monkeypatch.setattr(lsp_process_state.ProcessTree, "terminate", block_fresh_cleanup)
    caller = threading.Thread(target=restart)
    caller.start()
    try:
        waited = (
            evidence_started.wait(LONG_TIMEOUT),
            cleanup_started.wait(LONG_TIMEOUT),
            caller_finished.wait(SHORT_TIMEOUT),
        )
        # The event is set in the thread's `finally`, just before it leaves `run()`.
        caller.join(SHORT_TIMEOUT)
        assert (recovery is not None, waited, caller.is_alive(), [type(error) for error in restart_errors]) == (
            True, (True, True, True), False, [OSError],
        )
        assert (
            str(restart_errors[0]),
            restart_elapsed[0] < 20,
            cleanup_threads[0] is caller,
            cleanup_deadlines[0] == caller_deadline,
        ) == ("caller restart candidate failed", True, True, True)

        assert (
            autonomous_cleanup_started.wait(LONG_TIMEOUT),
            recovery in cleanup_threads[1:],
            recovery.is_alive(),
            lsp_process_state.coordinator_has_ownership(coordinator),
        ) == (True, True, True, True)

        allow_autonomous_cleanup.set()
        stopped = _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=120,
        )
        recovery.join(LONG_TIMEOUT)
        assert (stopped, recovery.is_alive(), lsp_process_state.coordinator_has_ownership(coordinator)) == (
            True, False, False,
        )
    finally:
        _release_restart_caller(process, coordinator, caller, allow_autonomous_cleanup)


def _release_restart_caller(process, coordinator, caller, allow_autonomous_cleanup) -> None:
    allow_autonomous_cleanup.set()
    caller.join(LONG_TIMEOUT)
    if lsp_process_state.coordinator_has_ownership(coordinator):
        process.close(time.monotonic() + 120)


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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    coordinator = process._coordinator
    first_protocol = process.protocol
    real_restart_generation = lsp_process_restart.restart_generation
    real_process_recovery_request = lsp_process_recovery.process_recovery_request
    explicit_paused = threading.Event()
    release_explicit = threading.Event()
    autonomous_entered = threading.Event()
    autonomous_replacement_entered = threading.Event()
    autonomous_replacement_finished = threading.Event()
    explicit_threads: list[threading.Thread] = []
    explicit_errors: list[BaseException] = []

    def note_restart_entry(current: threading.Thread, deadline: float) -> None:
        if current is explicit_threads[0]:
            explicit_paused.set()
            assert release_explicit.wait(max(0.0, deadline - time.monotonic()))
            return
        if current is coordinator.recovery_thread:
            autonomous_replacement_entered.set()

    def pause_explicit_restart(instance: LspProcess, deadline: float) -> None:
        current = threading.current_thread()
        note_restart_entry(current, deadline)
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

    monkeypatch.setattr(lsp_process_restart, "restart_generation", pause_explicit_restart)
    monkeypatch.setattr(
        lsp_process_recovery, "process_recovery_request", observe_autonomous_recovery
    )
    restart_thread = threading.Thread(target=explicit_restart)
    explicit_threads.append(restart_thread)
    restart_thread.start()
    try:
        assert explicit_paused.wait(SHORT_TIMEOUT)
        first_protocol._become_fatal("fatal while explicit restart is pending")
        coordinator.recovery_wake.set()
        assert autonomous_entered.wait(LONG_TIMEOUT)
        if autonomous_replacement_entered.wait(0.25):
            assert autonomous_replacement_finished.wait(SHORT_TIMEOUT)

        release_explicit.set()
        restart_thread.join(SHORT_TIMEOUT)

        assert not restart_thread.is_alive()
        assert explicit_errors == []
        assert bootstraps == [
            bootstraps[0],
            (process.process.pid, process.generation_nonce),
        ]
        assert len(bootstraps) == 2
        assert process.restart_count == 1
        assert process.state is ProcessState.WORKSPACE_READY
        assert coordinator.phase is lsp_process_state.LifecyclePhase.RUNNING
        assert coordinator.active is not None
        assert coordinator.candidate is None
        assert coordinator.terminal_outcome is None
    finally:
        release_explicit.set()
        restart_thread.join(SHORT_TIMEOUT)
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_bootstrap_raw_protocol_and_handler_complete_while_driver_is_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinators: list[lsp_process_state.LifecycleCoordinator] = []
    configurations: list[object] = []
    lock_observations: list[tuple[bool, bool, bool]] = []
    real_prepare = lsp_process_generation.prepare_generation

    def observe_prepare(
        coordinator: lsp_process_state.LifecycleCoordinator,
        *args: object,
        **kwargs: object,
    ) -> lsp_process_state.Generation:
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
            ).result(timeout=SHORT_TIMEOUT)
            lifecycle_available = pool.submit(
                lock_is_available, coordinator.lock
            ).result(timeout=SHORT_TIMEOUT)
            lease_available = pool.submit(
                lock_is_available, coordinator.lease_lock
            ).result(timeout=SHORT_TIMEOUT)
        lock_observations.append(
            (driver_available, lifecycle_available, lease_available)
        )
        assert driver_available is False
        assert lifecycle_available is True
        assert lease_available is True
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    monkeypatch.setattr(lsp_process_generation, "prepare_generation", observe_prepare)
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
    real_start_heartbeat = lsp_process_recovery.start_heartbeat_worker
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
                return
            if operation == "restart":
                instance.restart(deadline)
                return
            instance._terminal_failure("handler_failure", deadline)
        except BaseException as error:
            handler_errors.append(error)
        finally:
            handler_elapsed.append(time.monotonic() - started)
            lifecycle_snapshots.append(snapshot(instance))

    monkeypatch.setattr(lsp_process_recovery, "start_heartbeat_worker", capture_instance)
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / owner_nonce,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        assert process._coordinator.phase is lsp_process_state.LifecyclePhase.RUNNING
        assert process._coordinator.terminal_outcome is None
        assert process._coordinator.mandatory_failure_intent is None
    finally:
        process.close(time.monotonic() + 5)


def test_uncaught_progress_handler_lifecycle_error_is_nonfatal_and_bootstrap_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instances: list[LspProcess] = []
    real_start_heartbeat = lsp_process_recovery.start_heartbeat_worker
    handler_entered = threading.Event()
    handler_returned_normally = threading.Event()

    def capture_instance(instance: LspProcess, deadline: float) -> None:
        instances.append(instance)
        real_start_heartbeat(instance, deadline)

    def progress(_params: object) -> None:
        handler_entered.set()
        instances[0].close(time.monotonic() + 5)
        handler_returned_normally.set()

    monkeypatch.setattr(lsp_process_recovery, "start_heartbeat_worker", capture_instance)
    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / ("e" * 32),
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": progress},
        generation_bootstrap=_initialize_generation,
    )
    try:
        assert handler_entered.is_set()
        assert not handler_returned_normally.is_set()
        assert process.protocol.fatal is False
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert process._coordinator.phase is lsp_process_state.LifecyclePhase.RUNNING
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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
        if lsp_process_state.coordinator_has_ownership(process._coordinator):
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
            deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
            server_request_handlers={"workspace/configuration": request_handler},
            server_notification_handlers={"$/progress": lambda _params: None},
            generation_bootstrap=_initialize_generation,
        )

        assert callback_finished.is_set()
        assert callback_errors == []
        assert len(callback_elapsed) == 1
        assert source.state is ProcessState.PROTOCOL_INITIALIZED
        assert source.protocol.fatal is False
        assert not lsp_process_state.coordinator_has_ownership(other._coordinator)
    finally:
        if source is not None and lsp_process_state.coordinator_has_ownership(
            source._coordinator
        ):
            source.close(time.monotonic() + 5)
        if lsp_process_state.coordinator_has_ownership(other._coordinator):
            other.close(time.monotonic() + 5)


def test_protocol_callback_markers_restore_nested_scopes_and_exceptions() -> None:
    outer_coordinator = lsp_process_state.LifecycleCoordinator(None)
    inner_coordinator = lsp_process_state.LifecycleCoordinator(None)
    outer_process = object.__new__(LspProcess)
    inner_process = object.__new__(LspProcess)
    outer_process._coordinator = outer_coordinator
    inner_process._coordinator = inner_coordinator
    handler_error = ValueError("user handler failed")

    def inner_handler(_params: object) -> None:
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process_launch.reject_protocol_callback_lifecycle(outer_process)
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process_launch.reject_protocol_callback_lifecycle(inner_process)
        raise handler_error

    wrapped_inner = lsp_process_launch.wrap_protocol_callback(
        inner_coordinator, inner_handler
    )

    def outer_handler(params: object) -> object:
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process_launch.reject_protocol_callback_lifecycle(outer_process)
        with pytest.raises(ValueError) as raised:
            wrapped_inner(params)
        assert raised.value is handler_error
        with pytest.raises(RuntimeError, match="not reentrant"):
            lsp_process_launch.reject_protocol_callback_lifecycle(outer_process)
        lsp_process_launch.reject_protocol_callback_lifecycle(inner_process)
        raise handler_error

    wrapped_outer = lsp_process_launch.wrap_protocol_callback(
        outer_coordinator, outer_handler
    )
    with pytest.raises(ValueError) as raised:
        wrapped_outer(object())
    assert raised.value is handler_error
    lsp_process_launch.reject_protocol_callback_lifecycle(outer_process)
    lsp_process_launch.reject_protocol_callback_lifecycle(inner_process)


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
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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
    assert bootstrap_entered.wait(LONG_TIMEOUT)
    close_thread.start()
    close_returned_before_bootstrap = close_finished.wait(0.2)
    release_bootstrap.set()
    restart_thread.join(SHORT_TIMEOUT)
    close_thread.join(SHORT_TIMEOUT)
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
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
        assert coordinator.cleanup_result.evidence == "not_applicable"
        assert coordinator.cleanup_result.ownership_pending is False
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
        assert not process.owner_root.exists()
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        release_bootstrap.set()
        restart_thread.join(SHORT_TIMEOUT)
        close_thread.join(SHORT_TIMEOUT)
        if lsp_process_state.coordinator_has_ownership(coordinator):
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
    candidate = lsp_process_state.Generation(
        "b" * 32,
        _FakeTree(candidate_process),  # type: ignore[arg-type]
        candidate_process,
    )
    with coordinator.condition:
        coordinator.candidate = candidate
    callback_errors: list[BaseException] = []

    def delayed_failure_callback() -> None:
        try:
            lsp_process_failure.mark_terminal_failure(
                process,
                coordinator,
                "process_exited",
                time.monotonic() + 2,
            )
            lsp_process_restart.fail_restart_generation(process, time.monotonic() + 2)
        except BaseException as error:
            callback_errors.append(error)

    callback = threading.Thread(target=delayed_failure_callback)
    callback.start()
    callback.join(SHORT_TIMEOUT)

    assert not callback.is_alive()
    assert callback_errors == []
    assert coordinator.success_committed is True
    assert coordinator.terminal_outcome == "success"
    assert coordinator.terminal_code is None
    assert coordinator.failure_evidence_identity is None
    assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
    assert coordinator.cleanup_result.evidence == "not_applicable"
    assert coordinator.cleanup_result.ownership_pending is False
    assert coordinator.candidate is None
    assert candidate.released
    assert candidate_process.poll() is not None
    assert process.state is state_before_close
    assert not process.owner_root.exists()
    assert not lsp_process_state.coordinator_has_ownership(coordinator)


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
            assert release_bootstrap.wait(SHORT_TIMEOUT)
        return _initialize_generation(protocol, pid, generation_nonce, deadline)

    process = LspProcess.start_configured(
        _command("--lifecycle", "--bootstrap-handshake"),
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
        deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
        server_request_handlers={"workspace/configuration": lambda _params: True},
        server_notification_handlers={"$/progress": lambda _params: None},
        generation_bootstrap=bootstrap,
    )
    first_nonce = process.generation_nonce
    real_write_lease = lsp_process_generation.write_generation_lease
    heartbeat_generations: list[str] = []
    restart_errors: list[BaseException] = []

    def observe_write_lease(
        owner: lsp_process_owner.OwnerDirectory,
        generation: lsp_process_state.Generation,
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

    monkeypatch.setattr(lsp_process_generation, "write_generation_lease", observe_write_lease)
    thread = threading.Thread(target=restart)
    thread.start()
    try:
        assert bootstrap_entered.wait(LONG_TIMEOUT)
        candidate_nonce = bootstraps[1]
        process._coordinator.heartbeat_wake.set()
        assert heartbeat_wrote.wait(_BARRIER_SECONDS)
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
            lsp_process_state.LifecyclePhase.RESTARTING,
            None,
            candidate_nonce,
            candidate_nonce,
        )
        assert process.generation_nonce == first_nonce
        assert process.state is ProcessState.DEGRADED
        assert candidate_nonce != first_nonce

        release_bootstrap.set()
        thread.join(SHORT_TIMEOUT)
        assert not thread.is_alive()
        assert restart_errors == []
        assert process.generation_nonce == candidate_nonce
    finally:
        release_bootstrap.set()
        thread.join(SHORT_TIMEOUT)
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
        assert time.monotonic() - started < lsp_process_limits.STARTUP_WAIT_SECONDS
        assert process._coordinator.generation_configuration.generation_bootstrap is None
        assert process._coordinator.generation_configuration.generation_guard is None
    finally:
        process.close(time.monotonic() + 5)


def test_lifecycle_coordinator_uses_one_authority_lock_and_condition() -> None:
    coordinator = lsp_process_state.LifecycleCoordinator(None)

    assert coordinator.condition._lock is coordinator.lock
    assert coordinator.driver is not coordinator.lock
    assert not hasattr(coordinator, "driver_owner")
    assert not hasattr(coordinator, "driver_depth")
