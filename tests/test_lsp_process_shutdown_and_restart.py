"""Shutdown, close and fatal restart of an LSP child stay bounded and owned."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lsp_process
import lsp_process_tree
import lsp_protocol
import pytest
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_failure,
    lsp_process_requests,
    lsp_process_state,
)
from lsp_process import (
    LspProcess,
    ProcessState,
)
from lsp_protocol import (
    CancellationSource,
    JsonRpcResponseError,
    ProtocolViolation,
    RequestCancelled,
)

from tests.lsp_process_helpers import (
    _no_lsp_lifecycle_owner_leaks,  # noqa: F401
    _poll_until_empty,
)
from tests.lsp_process_scenario_helpers import (
    _BARRIER_SECONDS,
    _COMPLETES_WHILE_HELD_SECONDS,
    _DEADLINE_SECONDS,
    _HOLD_SECONDS,
    _STARTUP_BUDGET_SECONDS,
    _await_descendant_pid,
    _await_pid_exit,
    _close_if_owned,
    _command,
    _coordinator_wait,
    _lock_is_free,
    _owner_entry_names,
    _pid_alive,
    _start,
    _windows_handle_status,
)
from tests.slow_machine import SHORT_TIMEOUT


def _join_started(thread, timeout: float = 5) -> None:
    if thread.ident is not None:
        thread.join(timeout)


def _await_pending_request(process, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while process.protocol.pending_count == 0 and time.monotonic() < deadline:
        time.sleep(0.001)


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


def _await_threads_gone(names: set[str], timeout: float = 60.0) -> list[str]:
    """Named worker threads that never leave; an empty list means none leaked."""
    return _poll_until_empty(lambda: _live_threads(names), timeout)


def _live_threads(names: set[str]) -> list[str]:
    return [thread.name for thread in threading.enumerate() if thread.name in names]


def _await_restart_with_lease(process, lease_path: Path, deadline: float) -> None:
    """Wait for the replacement generation to publish its lease."""
    while time.monotonic() < deadline:
        if process.restart_count > 0 and lease_path.exists():
            return
        time.sleep(0.01)


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
        generations = lsp_process_state.generations_locked(coordinator)
        assert any(generation.tree is not None for generation in generations), live
        owner = coordinator.owner_directory
        assert owner is not None and not owner._closed, live
        assert (process.owner_root / "lease.json").is_file(), live


def test_driver_release_survives_transition_contention_and_cleanup_reacquires() -> None:
    coordinator = lsp_process_state.LifecycleCoordinator(None)
    owner_acquired = threading.Event()
    held = threading.Event()
    release_owner = threading.Event()
    release_transition = threading.Event()
    owner_done = threading.Event()
    owner_errors: list[BaseException] = []

    def own_driver() -> None:
        try:
            lsp_process_state.acquire_driver(coordinator, time.monotonic() + 1)
            owner_acquired.set()
            assert release_owner.wait(_HOLD_SECONDS)
        except BaseException as error:
            owner_errors.append(error)
        finally:
            try:
                lsp_process_state.release_driver(coordinator)
            except BaseException as error:
                owner_errors.append(error)
            owner_done.set()

    def hold_lifecycle_lock() -> None:
        with coordinator.condition:
            held.set()
            assert release_transition.wait(_HOLD_SECONDS)

    owner = threading.Thread(target=own_driver)
    owner.start()
    assert owner_acquired.wait(_BARRIER_SECONDS)
    holder = threading.Thread(target=hold_lifecycle_lock)
    holder.start()
    assert held.wait(_BARRIER_SECONDS)
    expired = time.monotonic() + _DEADLINE_SECONDS
    try:
        assert threading.Event().wait(max(0.0, expired - time.monotonic())) is False
        release_owner.set()
        watch_started = time.monotonic()
        released_while_held = owner_done.wait(_COMPLETES_WHILE_HELD_SECONDS)
        assert released_while_held is True, (
            f"the driver owner never finished while the lifecycle lock was held: "
            f"waited {time.monotonic() - watch_started:.3f}s of "
            f"{_COMPLETES_WHILE_HELD_SECONDS}s; driver free="
            f"{_lock_is_free(coordinator.driver)}; owner alive={owner.is_alive()}; "
            f"errors={owner_errors!r}"
        )
    finally:
        release_transition.set()
        holder.join(_BARRIER_SECONDS)
        owner.join(_BARRIER_SECONDS)

    assert not holder.is_alive()
    assert not owner.is_alive()
    retry_errors = lsp_process_cleanup.drive_cleanup(
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

    # The start identities name who a pid meant, so a reused pid reads as dead (C-39).
    assert set(lease) == {
        "expires_at",
        "generation_nonce",
        "heartbeat_at",
        "manager_pid",
        "manager_start_identity",
        "owner_nonce",
        "schema_version",
        "server_pid",
        "server_start_identity",
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
        lsp_process_tree,
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
    assert retained.wait(timeout=SHORT_TIMEOUT) == retained.returncode


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
    real_unregister = atexit.unregister
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
    monkeypatch.setattr(atexit, "unregister", unregister)
    try:
        with pytest.raises(OSError, match="process handle close failed"):
            process.close(time.monotonic() + 5)

        assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
        assert coordinator.active is generation
        assert generation.tree is not None
        assert generation.process is retained
        assert coordinator.cleanup_result.tree_release == "failed"
        assert (process.owner_root / "lease.json").is_file()
        assert _windows_handle_status(handle) == (True, 0)
        assert unregister_calls == []

        close_allowed = True
        process.close(time.monotonic() + 5)

        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
        assert coordinator.cleanup_result.tree_release == "success"
        assert _windows_handle_status(handle) == (False, 6)
        assert retained.poll() == retained.returncode
        assert len(unregister_calls) == 1
        assert not process.owner_root.exists()
    finally:
        close_allowed = True
        if lsp_process_state.coordinator_has_ownership(coordinator):
            process.close(time.monotonic() + 5)


def test_request_after_shutdown_cannot_restart_terminal_process(tmp_path: Path) -> None:
    process = _start(tmp_path, "--lifecycle")
    pid = process.process.pid
    generation = process.generation_nonce

    process.shutdown(time.monotonic() + 5)

    with pytest.raises(RuntimeError, match="LSP process is closed"):
        process.request("echo", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert process.process.pid == pid
    assert process.generation_nonce == generation
    assert process.restart_count == 0


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
    _await_pid_exit(descendant)
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
    drive_cleanup = lsp_process_cleanup.drive_cleanup
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
    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", cleanup_then_report_ordinary)

    with pytest.raises(
        KeyboardInterrupt,
        match="graceful shutdown interrupted",
    ) as raised:
        process.shutdown(time.monotonic() + 5)

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.__cause__ is ordinary_error
    assert not lsp_process_state.coordinator_has_ownership(process._coordinator)
    assert not process.owner_root.exists()


def test_shutdown_unwraps_graceful_interruption_without_exception_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    protocol = process.protocol
    request = lsp_protocol.LspProtocol.request
    drive_cleanup = lsp_process_cleanup.drive_cleanup
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
    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", cleanup_then_report_ordinary)

    with pytest.raises(
        KeyboardInterrupt,
        match="wrapped graceful shutdown interruption",
    ) as raised:
        process.shutdown(time.monotonic() + 5)

    assert raised.value is interruption
    assert raised.value.__cause__ is ordinary_error
    assert raised.value.__context__ is None
    assert wrapper.__cause__ is interruption
    assert not lsp_process_state.coordinator_has_ownership(process._coordinator)
    assert not process.owner_root.exists()


def test_shutdown_later_cleanup_interruption_outranks_first_ordinary_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle", "--sleep-seconds", "30")
    drive_cleanup = lsp_process_cleanup.drive_cleanup
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

    monkeypatch.setattr(lsp_process_cleanup, "drive_cleanup", cleanup_then_return_errors)

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
    assert not lsp_process_state.coordinator_has_ownership(process._coordinator)
    assert not process.owner_root.exists()


def test_fatal_request_restarts_once_with_fresh_generation(tmp_path: Path) -> None:
    marker = tmp_path / "crashed"
    process = _start(tmp_path, "--lifecycle", "--crash-once-marker", str(marker))
    first_generation = process.generation_nonce

    assert process.request("echo", {"ok": True}, deadline=time.monotonic() + SHORT_TIMEOUT) == {
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
        (lsp_process_state.LifecyclePhase.RUNNING, "success"),
        (lsp_process_state.LifecyclePhase.STOPPING_SUCCESS, None),
        (lsp_process_state.LifecyclePhase.STOPPING_FAILURE, None),
        (lsp_process_state.LifecyclePhase.CLEANUP_PENDING, None),
        (lsp_process_state.LifecyclePhase.STOPPED_SUCCESS, None),
        (lsp_process_state.LifecyclePhase.STOPPED_FAILURE, None),
    )
    try:
        for phase, terminal_outcome in cases:
            _set_lifecycle_phase(coordinator, phase, terminal_outcome)
            assert lsp_process_requests.wait_for_generation_change(
                process,
                process.generation_nonce,
                time.monotonic() + 0.02,
            ) == (
                False,
                None,
            )
    finally:
        _set_lifecycle_phase(coordinator, lsp_process_state.LifecyclePhase.RUNNING, None)
        process.close(time.monotonic() + 5)


def _set_lifecycle_phase(coordinator, phase, terminal_outcome) -> None:
    with coordinator.condition:
        coordinator.phase = phase
        coordinator.terminal_outcome = terminal_outcome


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
            process.request("echo", {}, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert _coordinator_wait(
            process,
            lambda: coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            timeout=5,
        )
        recovery.join(SHORT_TIMEOUT)
        heartbeat.join(SHORT_TIMEOUT)
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
        assert not lsp_process_state.coordinator_has_ownership(coordinator)
    finally:
        if lsp_process_state.coordinator_has_ownership(coordinator):
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
    assert process._coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
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
    real_close = lsp_process_tree._close_windows_process_handle

    def close_process_handle(child: subprocess.Popen[bytes]) -> None:
        real_close(child)
        if child is old:
            close_observations.append(_windows_handle_status(old_handle))

    monkeypatch.setattr(
        lsp_process_tree,
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
    lsp_process_failure.stop_recovery_owner(coordinator, time.monotonic() + 2)

    exit_observed = threading.Event()
    release_callbacks = threading.Event()
    accepted_intents: list[str] = []
    shutdown_errors: list[BaseException] = []
    real_queue_failure = lsp_process_state.queue_generation_failure
    real_enqueue = lsp_process_state.enqueue_failure_intent
    real_become_fatal = protocol._become_fatal

    def gated_queue_failure(
        current: lsp_process_state.LifecycleCoordinator,
        target: lsp_process_state.Generation,
        reason: str,
    ) -> bool:
        if current is coordinator and target is generation:
            if threading.current_thread() is generation.exit_thread:
                exit_observed.set()
            assert release_callbacks.wait(SHORT_TIMEOUT)
        return real_queue_failure(current, target, reason)

    def gated_become_fatal(
        reason: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        if threading.current_thread() is generation.exit_thread:
            exit_observed.set()
        assert release_callbacks.wait(SHORT_TIMEOUT)
        real_become_fatal(reason, cause=cause)

    def record_enqueue(
        current: lsp_process_state.LifecycleCoordinator,
        intent: lsp_process_state.FailureIntent,
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

    monkeypatch.setattr(lsp_process_state, "queue_generation_failure", gated_queue_failure)
    monkeypatch.setattr(lsp_process_state, "enqueue_failure_intent", record_enqueue)
    monkeypatch.setattr(protocol, "_become_fatal", gated_become_fatal)
    closer = threading.Thread(target=shutdown)
    try:
        process.process.kill()
        assert exit_observed.wait(SHORT_TIMEOUT)
        closer.start()
        assert _coordinator_wait(
            process,
            lambda: coordinator.phase
            in {
                lsp_process_state.LifecyclePhase.STOPPING_SUCCESS,
                lsp_process_state.LifecyclePhase.STOPPING_FAILURE,
                lsp_process_state.LifecyclePhase.CLEANUP_PENDING,
                lsp_process_state.LifecyclePhase.STOPPED_SUCCESS,
                lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
            },
        )
        release_callbacks.set()
        closer.join(SHORT_TIMEOUT)

        assert not closer.is_alive()
        assert shutdown_errors == []
        assert accepted_intents == [generation.nonce]
        assert process.restart_count == 1
        assert process.state is ProcessState.FAILED
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
        failure = json.loads((process.owner_root / "failure.json").read_bytes())
        lsp_process_state.validate_failure_record(
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
    lsp_process_failure.stop_recovery_owner(coordinator, time.monotonic() + 2)

    expected_exit_marked = threading.Event()
    release_shutdown_request = threading.Event()
    accepted_intents: list[str] = []
    shutdown_errors: list[BaseException] = []
    real_request = protocol.request
    real_enqueue = lsp_process_state.enqueue_failure_intent

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
            assert release_shutdown_request.wait(SHORT_TIMEOUT)
        return real_request(
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,
        )

    def record_enqueue(
        current: lsp_process_state.LifecycleCoordinator,
        intent: lsp_process_state.FailureIntent,
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
    monkeypatch.setattr(lsp_process_state, "enqueue_failure_intent", record_enqueue)
    closer = threading.Thread(target=shutdown)
    try:
        closer.start()
        assert expected_exit_marked.wait(SHORT_TIMEOUT)
        process.process.kill()
        process.process.wait(timeout=SHORT_TIMEOUT)
        release_shutdown_request.set()
        closer.join(SHORT_TIMEOUT)

        assert not closer.is_alive()
        assert shutdown_errors == []
        assert accepted_intents == []
        assert process.restart_count == 1
        assert coordinator.terminal_outcome == "success"
        assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_SUCCESS
        assert not process.owner_root.exists()
    finally:
        release_shutdown_request.set()
        if closer.ident is not None:
            closer.join(SHORT_TIMEOUT)
        if lsp_process_state.coordinator_has_ownership(coordinator):
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
            is lsp_process_state.LifecyclePhase.STOPPED_FAILURE,
        )
        assert process.state is ProcessState.FAILED
        assert process._tree is None
        assert (process.owner_root / "failure.json").is_file()
        assert not lease_path.exists()
    finally:
        first_gate.touch(exist_ok=True)
        second_gate.touch(exist_ok=True)
        if lsp_process_state.coordinator_has_ownership(process._coordinator):
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
            process.request("ending", {}, deadline=time.monotonic() + SHORT_TIMEOUT)
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
                deadline=time.monotonic() + _STARTUP_BUDGET_SECONDS,
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

    lsp_process_failure.mark_terminal_failure(
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

    _await_pid_exit(descendant)
    assert not _pid_alive(descendant)
    assert _await_threads_gone(thread_names) == []


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
    _await_pid_exit(descendant)
    assert not _pid_alive(descendant)
    assert not owner.exists()
