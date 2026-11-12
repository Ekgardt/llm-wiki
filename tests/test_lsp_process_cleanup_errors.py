"""Cleanup errors and heartbeat failures of an LSP child are reported, not hidden."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_failure,
    lsp_process_generation,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_recovery,
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
    _COMPLETES_WHILE_HELD_SECONDS,
    _DEADLINE_SECONDS,
    _HOLD_SECONDS,
    _coordinator_wait,
    _lock_is_free,
    _poll_until,
    _start,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


def _lease_heartbeat(lease_path: Path, timeout: float = 30.0) -> str:
    """The lease's heartbeat stamp, waiting out a writer that holds the file.

    On Windows the heartbeat holds lease.json exclusively while rewriting it,
    so an eager read raises PermissionError and a partial read is not JSON.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stamp = _read_lease_heartbeat(lease_path)
        if stamp is not None:
            return stamp
        time.sleep(0.01)
    raise AssertionError(f"lease heartbeat was never readable in {lease_path}")


def _read_lease_heartbeat(lease_path: Path) -> str | None:
    """One attempt at the heartbeat stamp; None while the writer holds the file."""
    try:
        record = json.loads(lease_path.read_bytes())
    except (OSError, ValueError):
        return None
    stamp = record.get("heartbeat_at")
    return stamp if isinstance(stamp, str) else None


def _await_new_heartbeat(
    lease_path: Path, previous: str, timeout: float = 30.0
) -> str:
    """Wait for the heartbeat to move past the stamp we already saw."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stamp = _read_lease_heartbeat(lease_path)
        if stamp is not None and stamp != previous:
            return stamp
        time.sleep(0.01)
    raise AssertionError(f"lease heartbeat never moved past {previous}")


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
            assert release.wait(_HOLD_SECONDS)

    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert held.wait(_BARRIER_SECONDS)

    callback = threading.Thread(
        target=lambda: (
            process.protocol._become_fatal("injected fatal"),
            callback_returned.set(),
        )
    )
    callback.start()
    watch_started = time.monotonic()
    returned_while_held = callback_returned.wait(_COMPLETES_WHILE_HELD_SECONDS)
    assert returned_while_held is True, (
        f"the fatal callback never returned while the transition lock was held: "
        f"waited {time.monotonic() - watch_started:.3f}s of "
        f"{_COMPLETES_WHILE_HELD_SECONDS}s; transition lock free="
        f"{_lock_is_free(coordinator.lock)}; callback alive={callback.is_alive()}"
    )
    release.set()
    holder.join(_BARRIER_SECONDS)
    callback.join(_BARRIER_SECONDS)

    assert _coordinator_wait(process, lambda: process.restart_count == 1)
    assert process.request("echo", {"ok": True}, deadline=time.monotonic() + SHORT_TIMEOUT) == {
        "ok": True
    }
    assert process.restart_count == 1
    process.close(time.monotonic() + 5)


def test_shutdown_waits_for_restart_candidate_ownership_to_be_attached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    real_spawn = lsp_process_state.ProcessTree._spawn_with_deadline.__func__
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
        assert release_spawn.wait(SHORT_TIMEOUT)
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
        lsp_process_state.ProcessTree,
        "_spawn_with_deadline",
        classmethod(blocked_spawn),
    )
    restart_thread = threading.Thread(target=restart)
    shutdown_thread = threading.Thread(target=shutdown)
    restart_thread.start()
    assert spawned.wait(SHORT_TIMEOUT)
    shutdown_thread.start()
    shutdown_returned_before_candidate_attachment = shutdown_finished.wait(0.2)
    release_spawn.set()
    restart_thread.join(SHORT_TIMEOUT)
    shutdown_thread.join(SHORT_TIMEOUT)

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
        assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING
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

    generation = lsp_process_state.Generation(
        "b" * 32,
        Tree(),  # type: ignore[arg-type]
        Process(),  # type: ignore[arg-type]
        protocol=Protocol(),  # type: ignore[arg-type]
    )
    coordinator = lsp_process_state.LifecycleCoordinator(
        None,
        phase=lsp_process_state.LifecyclePhase.STOPPING_FAILURE,
        active=generation,
        terminal_outcome="failure",
        terminal_code="injected_failure",
    )

    errors = lsp_process_cleanup.drive_cleanup(
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
    assert coordinator.phase is lsp_process_state.LifecyclePhase.CLEANUP_PENDING


def test_fatal_intent_after_cleanup_drain_dominates_success_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = _start(tmp_path, "--lifecycle")
    coordinator = process._coordinator
    server_pid = process.process.pid
    real_drain = lsp_process_failure.drain_terminal_failures
    injected = False
    accepted: bool | None = None

    def drain_then_inject(
        instance: LspProcess | None,
        current: lsp_process_state.LifecycleCoordinator,
        deadline: float,
    ) -> None:
        nonlocal accepted, injected
        real_drain(instance, current, deadline)
        if current is coordinator and not injected:
            injected = True
            accepted = lsp_process_state.queue_owner_failure(
                current, "fatal at finalization barrier"
            )

    monkeypatch.setattr(lsp_process_failure, "drain_terminal_failures", drain_then_inject)

    process.close(time.monotonic() + 5)

    assert injected is True
    assert accepted is True
    failure_path = process.owner_root / "failure.json"
    failure = json.loads(failure_path.read_bytes())
    lsp_process_state.validate_failure_record(
        failure,
        code="heartbeat_failed",
        owner_nonce=process.owner_nonce,
        generation_nonce=process.generation_nonce,
        pid=server_pid,
    )
    assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
    assert coordinator.cleanup_result.evidence == "success"
    assert not (process.owner_root / "lease.json").exists()
    assert not lsp_process_state.coordinator_has_ownership(coordinator)


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

    result = lsp_process_state.CleanupResult()

    for index in range(100):
        try:
            raise OSError(5, f"D:/private/repository-{index}")
        except OSError as error:
            result.failed("tree_termination", error)

    assert len(result.errors) == 1
    assert len(result.errors) <= lsp_process_limits.MAX_RETAINED_CLEANUP_ERRORS
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

    assert result.errors[-1] == lsp_process_state.CleanupError(
        step="protocol_stop", error_type="OSError", error_code=None
    )

    result.failed("evidence", OversizedCodeFailure())

    assert result.errors[-1] == lsp_process_state.CleanupError(
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
    coordinator = lsp_process_state.LifecycleCoordinator(None)
    error = OSError(5, "D:/private/background-cleanup")

    lsp_process_state.remember_background_cleanup_error(coordinator, error)

    retained = coordinator.background_cleanup_error
    assert retained is not None
    assert retained.error_type == "OSError"
    assert retained.error_code == 5
    assert not hasattr(retained, "error")
    assert "private" not in repr(retained)
    replayed = lsp_process_state.take_background_cleanup_error(coordinator)
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
    first_timestamp = _lease_heartbeat(lease_path)
    terminate = lsp_process_state.ProcessTree.terminate
    fault_enabled = True

    def fail_tree(current: object, *, deadline: float) -> None:
        if current is tree and fault_enabled:
            raise OSError("tree terminate failed")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process_state.ProcessTree, "terminate", fail_tree)
    with pytest.raises((OSError, RuntimeError, TimeoutError), match="tree|live"):
        process.shutdown(time.monotonic() + 60)

    assert generation.tree is tree
    assert coordinator.owner_directory is not None
    assert (process.owner_root / "lease.json").is_file()
    assert (process.owner_root / "owner.json").is_file()
    assert coordinator.cleanup_result.ownership_pending is True
    assert heartbeat.is_alive()
    coordinator.heartbeat_wake.set()
    _await_new_heartbeat(lease_path, first_timestamp)

    fault_enabled = False
    process.shutdown(time.monotonic() + 60)
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
    terminate = lsp_process_state.ProcessTree.terminate
    tree_fault = True

    def terminate_tree(current: object, *, deadline: float) -> None:
        if current is tree and tree_fault:
            raise OSError("tree remains live")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process_state.ProcessTree, "terminate", terminate_tree)
    with pytest.raises(OSError, match="tree remains live"):
        process.close(time.monotonic() + 60)

    lease_path = process.owner_root / "lease.json"
    stale_lease = lease_path.read_bytes()

    def fail_pending_heartbeat(_instance: LspProcess, _deadline: float) -> None:
        raise OSError("cleanup-pending heartbeat failed")

    monkeypatch.setattr(lsp_process_generation, "write_current_lease", fail_pending_heartbeat)
    coordinator.heartbeat_wake.set()
    heartbeat.join(LONG_TIMEOUT)
    assert not heartbeat.is_alive()
    assert lease_path.read_bytes() == stale_lease
    assert coordinator.background_cleanup_error is not None
    assert coordinator.background_cleanup_error.error_type == "OSError"
    assert process.state is ProcessState.DEGRADED

    tree_fault = False
    with pytest.raises(RuntimeError, match=r"background cleanup failed \(OSError\)"):
        process.close(time.monotonic() + 60)

    failure = json.loads((process.owner_root / "failure.json").read_bytes())
    assert failure["code"] == "heartbeat_failed"
    assert not lease_path.exists()
    assert coordinator.phase is lsp_process_state.LifecyclePhase.STOPPED_FAILURE
    assert not lsp_process_state.coordinator_has_ownership(coordinator)


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
    terminate = lsp_process_state.ProcessTree.terminate
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
        lsp_process_state.ProcessTree,
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
    real_write_lease = lsp_process_owner.OwnerDirectory.write_lease

    def fail_heartbeat(current: object, record: object, **kwargs: object) -> None:
        if current is owner:
            attempted.set()
            raise OSError("heartbeat write failed")
        real_write_lease(current, record, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", fail_heartbeat)
    coordinator.heartbeat_wake.set()
    assert attempted.wait(SHORT_TIMEOUT)
    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process_state.LifecyclePhase.RUNNING,
    )
    heartbeat = coordinator.heartbeat_thread
    assert not (
        coordinator.phase is lsp_process_state.LifecyclePhase.RUNNING
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
    real_process_intent = lsp_process_recovery.process_failure_intent

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
        assert release_recovery.wait(SHORT_TIMEOUT)
        real_process_intent(current, intent, deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process_owner.OwnerDirectory, "write_lease", fail_heartbeat)
    monkeypatch.setattr(lsp_process_recovery, "process_failure_intent", block_recovery)
    coordinator.heartbeat_wake.set()
    try:
        assert write_attempted.wait(SHORT_TIMEOUT)
        assert recovery_entered.wait(LONG_TIMEOUT)
        heartbeat.join(SHORT_TIMEOUT)
        assert not heartbeat.is_alive()
        assert coordinator.phase is not lsp_process_state.LifecyclePhase.RUNNING
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
            assert release.wait(_HOLD_SECONDS)

    def fail_heartbeat(_instance: LspProcess, _deadline: float) -> None:
        attempted.set()
        raise OSError("heartbeat write failed under contention")

    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", 0.05)
    monkeypatch.setattr(lsp_process_generation, "write_current_lease", fail_heartbeat)
    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert held.wait(_BARRIER_SECONDS)
    coordinator.heartbeat_wake.set()

    try:
        assert attempted.wait(SHORT_TIMEOUT)
        heartbeat.join(SHORT_TIMEOUT)
        assert not heartbeat.is_alive()
        assert coordinator.pending_failure_intents >= 1
        # The bounded TimeoutError is recorded by the recovery loop, and only
        # while this thread still holds the transition lock. Waiting for it
        # here keeps the later `close()` assertion from depending on how
        # quickly that thread is scheduled.
        assert _poll_until(lambda: coordinator.background_cleanup_error is not None)
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process_state.LifecyclePhase.RUNNING,
    )
    with pytest.raises(RuntimeError, match=r"background cleanup failed \(TimeoutError\)"):
        process.close(time.monotonic() + 5)
    if lsp_process_state.coordinator_has_ownership(coordinator):
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
            assert release.wait(_HOLD_SECONDS)

    def inspect() -> None:
        try:
            results.append(lsp_process_generation.active_drain_generation(process))
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    monkeypatch.setattr(lsp_process_limits, "GRACEFUL_CLEANUP_SECONDS", _DEADLINE_SECONDS)
    holder = threading.Thread(target=hold_transition)
    holder.start()
    assert held.wait(_BARRIER_SECONDS)
    inspector = threading.Thread(target=inspect)
    watch_started = time.monotonic()
    inspector.start()
    try:
        completed_while_held = completed.wait(_COMPLETES_WHILE_HELD_SECONDS)
        assert completed_while_held is True, (
            f"the drain inspection never returned while the transition lock was "
            f"held: waited {time.monotonic() - watch_started:.3f}s of "
            f"{_COMPLETES_WHILE_HELD_SECONDS}s for a {_DEADLINE_SECONDS}s cleanup "
            f"budget; transition lock free={_lock_is_free(coordinator.lock)}; "
            f"inspector alive={inspector.is_alive()}; errors={errors!r}"
        )
        assert results == [None]
        assert errors == []
        assert coordinator.background_cleanup_error is not None
        assert coordinator.background_cleanup_error.error_type == "TimeoutError"
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)
        inspector.join(SHORT_TIMEOUT)

    assert _coordinator_wait(
        process,
        lambda: coordinator.phase is not lsp_process_state.LifecyclePhase.RUNNING,
    )
    with pytest.raises(RuntimeError, match=r"background cleanup failed \(TimeoutError\)"):
        process.close(time.monotonic() + 5)
    if lsp_process_state.coordinator_has_ownership(coordinator):
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
    lsp_process_state.queue_owner_failure(coordinator, "injected owner failure")

    assert failed.wait(SHORT_TIMEOUT)
    recovery = coordinator.recovery_thread
    assert recovery is not None
    recovery.join(SHORT_TIMEOUT)
    assert not recovery.is_alive()
    assert coordinator.cleanup_result.ownership_pending is False
    monkeypatch.setattr(protocol, "_stop_io_for_process_cleanup", stop_io)

    with pytest.raises(RuntimeError, match=r"background cleanup failed \(OSError\)"):
        process.close(time.monotonic() + 5)

    assert not lsp_process_state.coordinator_has_ownership(coordinator)
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
    real_write_lease = lsp_process_owner.OwnerDirectory.write_lease
    real_acquire_lease = lsp_process_state.acquire_lease
    restart_thread: threading.Thread | None = None

    def delayed_heartbeat(current: object, record: object, **kwargs: object) -> None:
        assert isinstance(record, dict)
        if (
            current is owner
            and threading.current_thread() is coordinator.heartbeat_thread
            and record["generation_nonce"] == first_generation
        ):
            heartbeat_writing.set()
            assert release_heartbeat.wait(SHORT_TIMEOUT)
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
        lsp_process_owner.OwnerDirectory,
        "write_lease",
        delayed_heartbeat,
    )
    monkeypatch.setattr(lsp_process_state, "acquire_lease", observe_lease_acquisition)
    coordinator.heartbeat_wake.set()
    assert heartbeat_writing.wait(SHORT_TIMEOUT)
    restart_thread = threading.Thread(target=restart)
    restart_thread.start()
    assert restart_acquiring_lease.wait(SHORT_TIMEOUT)
    assert not restart_finished.is_set()
    release_heartbeat.set()
    restart_thread.join(SHORT_TIMEOUT)

    assert not restart_thread.is_alive()
    assert restart_errors == []
    assert process.generation_nonce != first_generation
    lease = json.loads((process.owner_root / "lease.json").read_bytes())
    assert lease["generation_nonce"] == process.generation_nonce
    assert lease["server_pid"] == process.process.pid
    process.close(time.monotonic() + 5)
