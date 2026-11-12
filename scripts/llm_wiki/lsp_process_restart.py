"""Restarting an LSP child into a new generation."""
from __future__ import annotations

import contextlib
import threading
import time

from interruption import (
    raise_collected_errors as _raise_cleanup_failures,
)
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_generation,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_state,
)
from llm_wiki.lsp_process_failure import (
    mark_terminal_failure,
    remember_mandatory_terminal_failure,
)
from llm_wiki.lsp_process_generation import (
    bootstrapped_state,
    generation_guard_context,
    heartbeat_loop,
    publish_generation_lease,
)
from llm_wiki.lsp_process_launch import (
    generation_launch,
)
from llm_wiki.lsp_process_requests import (
    generation_has_channel,
)
from llm_wiki.lsp_process_state import (
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    acquire_driver,
    acquire_lifecycle,
    mark_generation_expected_exit,
    notify_lifecycle_locked,
    release_driver,
    remember_background_restart_error,
    require_startup_deadline,
)


def _adopt_recovery_ownership_locked(
    coordinator: LifecycleCoordinator,
) -> bool:
    """Take recovery ownership for this thread; False when someone else has it."""
    if not coordinator.cleanup_result.ownership_pending:
        return False
    current = threading.current_thread()
    owner = coordinator.recovery_thread
    if owner is not None and owner is not current and owner.is_alive():
        return False
    coordinator.recovery_thread = current
    coordinator.recovery_stop.clear()
    coordinator.cleanup_result.recovery_join = "pending"
    return True


def _adopt_heartbeat_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> threading.Thread | None:
    """A replacement heartbeat thread to start, when the old one is gone."""
    heartbeat_owner = coordinator.heartbeat_thread
    if heartbeat_owner is not None and heartbeat_owner.is_alive():
        return None
    coordinator.heartbeat_stop.clear()
    coordinator.heartbeat_wake.clear()
    heartbeat = threading.Thread(
        target=heartbeat_loop,
        args=(instance,),
        name=f"lsp-heartbeat-{instance.owner_nonce}",
        daemon=True,
    )
    coordinator.heartbeat_thread = heartbeat
    coordinator.cleanup_result.heartbeat_join = "pending"
    return heartbeat


def _retain_cleanup_owners_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> tuple[bool, threading.Thread | None]:
    """(retained, heartbeat to start) once the lifecycle lock is held."""
    if not _adopt_recovery_ownership_locked(coordinator):
        return False, None
    heartbeat = _adopt_heartbeat_locked(instance, coordinator)
    notify_lifecycle_locked(coordinator)
    return True, heartbeat


def _retain_cleanup_owners_driven(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, deadline: float
) -> bool:
    """Retain the owners while holding the driver; True when this thread owns them."""
    try:
        acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        return True
    try:
        retained, heartbeat = _retain_cleanup_owners_locked(instance, coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    if not retained:
        return False
    _start_retained_heartbeat(coordinator, heartbeat)
    return True


def _start_retained_heartbeat(
    coordinator: LifecycleCoordinator, heartbeat: threading.Thread | None
) -> None:
    if heartbeat is None:
        return
    try:
        heartbeat.start()
    except BaseException as error:
        lsp_process_state.remember_background_cleanup_error(coordinator, error)


def retain_autonomous_cleanup_owners(instance: LspProcessHandle) -> bool:
    coordinator = instance._coordinator
    deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    try:
        acquire_driver(coordinator, deadline)
    except TimeoutError:
        return True
    try:
        return _retain_cleanup_owners_driven(instance, coordinator, deadline)
    finally:
        release_driver(coordinator)


def restart_generation(
    instance: LspProcessHandle,
    deadline: float,
) -> None:
    coordinator = instance._coordinator
    driver_acquired = False
    try:
        acquire_driver(coordinator, deadline)
        driver_acquired = True
        candidate, generation_state = prepare_restart_generation_owned(
            instance, deadline
        )
        commit_restart_generation_owned(
            instance,
            candidate,
            generation_state,
            deadline,
        )
    except BaseException as restart_error:
        remember_background_restart_error(coordinator, restart_error)
        if driver_acquired:
            fail_restart_generation(instance, deadline)
        raise
    finally:
        if driver_acquired:
            release_driver(coordinator)


def _retire_active_generation_locked(coordinator: LifecycleCoordinator) -> None:
    """Move the running generation aside so a candidate can take its place."""
    if coordinator.terminal_outcome is not None:
        raise RuntimeError("LSP process is closed")
    old = coordinator.active
    if old is not None:
        mark_generation_expected_exit(old)
        coordinator.retired.append(old)
        coordinator.active = None
    coordinator.phase = LifecyclePhase.RESTARTING
    notify_lifecycle_locked(coordinator)


def _retire_for_restart(instance: LspProcessHandle, deadline: float) -> None:
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        _retire_active_generation_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _require_retirement_complete(
    coordinator: LifecycleCoordinator, retirement_errors: list[BaseException]
) -> None:
    """A restart may only proceed once every retired generation has let go."""
    released = all(item.released for item in coordinator.retired)
    if not retirement_errors and released:
        return
    if retirement_errors:
        _raise_cleanup_failures(retirement_errors)
    raise TimeoutError("LSP retired generation cleanup is incomplete")


def _require_restart_inputs(instance: LspProcessHandle) -> None:
    if instance._cwd is None or not instance._command:
        raise RuntimeError("LSP restart inputs are unavailable")


def _require_live_candidate(candidate: Generation) -> None:
    """A candidate that already died cannot be committed."""
    if not generation_has_channel(candidate):
        raise RuntimeError("LSP restart candidate failed before commit")
    if candidate.process.poll() is not None or candidate.protocol.fatal:
        raise RuntimeError("LSP restart candidate failed before commit")


def _launch_restart_candidate(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation_nonce: str,
    deadline: float,
) -> tuple[Generation, ProcessState]:
    """Start the replacement generation inside its guard and prove it is alive."""
    configuration = coordinator.generation_configuration
    with generation_guard_context(
        configuration, generation_nonce, deadline
    ) as guarded_launch:
        generation_command, pass_fds = generation_launch(
            instance._command,
            guarded_launch,
            cwd=instance._cwd,
            owner_root=instance.owner_root,
        )
        candidate = lsp_process_generation.prepare_generation(
            coordinator,
            generation_command,
            cwd=instance._cwd,
            environment=instance._environment,
            deadline=deadline,
            generation_nonce=generation_nonce,
            pass_fds=pass_fds,
        )
        _require_live_candidate(candidate)
        publish_generation_lease(instance, candidate, deadline)
        generation_state = bootstrapped_state(configuration, candidate, deadline)
    return candidate, generation_state


def prepare_restart_generation_owned(
    instance: LspProcessHandle,
    deadline: float,
) -> tuple[Generation, ProcessState]:
    coordinator = instance._coordinator
    _retire_for_restart(instance, deadline)
    _require_retirement_complete(
        coordinator, lsp_process_cleanup.drive_cleanup(instance, deadline, terminal=False)
    )
    _require_restart_inputs(instance)
    candidate, generation_state = _launch_restart_candidate(
        instance, coordinator, lsp_process_owner.new_generation_nonce(), deadline
    )
    require_startup_deadline(deadline, "generation guard completion")
    return candidate, generation_state


def _check_commit_preconditions_locked(
    coordinator: LifecycleCoordinator, candidate: Generation
) -> None:
    """Nothing may have moved between preparing the candidate and taking it."""
    if coordinator.terminal_outcome is not None:
        raise RuntimeError("LSP process became terminal during restart")
    if coordinator.candidate is not candidate:
        raise RuntimeError("LSP restart candidate lost lifecycle ownership")
    _require_unfailed_candidate(candidate)


def _require_unfailed_candidate(candidate: Generation) -> None:
    if candidate.process.poll() is not None or candidate.protocol.fatal:
        raise RuntimeError("LSP restart candidate failed before commit")


def _adopt_candidate_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    candidate: Generation,
    generation_state: ProcessState,
) -> None:
    """Make the candidate the running generation."""
    coordinator.active = candidate
    coordinator.candidate = None
    coordinator.lease_generation = candidate
    coordinator.phase = LifecyclePhase.RUNNING
    instance.process = candidate.process
    instance.protocol = candidate.protocol
    instance.generation_nonce = candidate.nonce
    instance.restart_count += 1
    instance.state = generation_state
    instance._stderr_projection = candidate.stderr
    instance._stderr_projection_lock = candidate.stderr_lock
    coordinator.recovery_request_nonce = None
    coordinator.recovery_request_pending.clear()
    notify_lifecycle_locked(coordinator)


def commit_restart_generation_owned(
    instance: LspProcessHandle,
    candidate: Generation,
    generation_state: ProcessState,
    deadline: float,
) -> None:
    coordinator = instance._coordinator
    if coordinator.owner_directory is None:
        raise RuntimeError("LSP owner directory is unavailable")
    _require_live_candidate(candidate)
    acquire_lifecycle(coordinator, deadline)
    try:
        _check_commit_preconditions_locked(coordinator, candidate)
        _adopt_candidate_locked(
            instance, coordinator, candidate, generation_state
        )
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _restart_failure_context(
    coordinator: LifecycleCoordinator, failure_deadline: float
) -> tuple[bool, bool]:
    """(a success is already committed, this thread owns recovery)."""
    acquire_lifecycle(coordinator, failure_deadline, allow_expired=True)
    try:
        return (
            coordinator.success_committed,
            coordinator.recovery_thread is threading.current_thread(),
        )
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _mark_restart_failure(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, failure_deadline: float
) -> None:
    """Record the terminal failure; a refusal here must not stop the cleanup."""
    remember_mandatory_terminal_failure(
        instance,
        coordinator,
        "restart_failed",
    )
    with contextlib.suppress(BaseException):
        mark_terminal_failure(
            instance,
            coordinator,
            "restart_failed",
            failure_deadline,
        )


def fail_restart_generation(instance: LspProcessHandle, failure_deadline: float) -> None:
    coordinator = instance._coordinator
    success_committed, recovery_owned = _restart_failure_context(
        coordinator, failure_deadline
    )
    if success_committed:
        lsp_process_cleanup.drive_cleanup(instance, failure_deadline, terminal=False)
        return
    if recovery_owned:
        failure_deadline = lsp_process_launch.fresh_cleanup_deadline()
    _mark_restart_failure(instance, coordinator, failure_deadline)
    _drive_restart_failure_cleanup(
        instance, coordinator, failure_deadline, recovery_owned=recovery_owned
    )


def _drive_restart_failure_cleanup(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    failure_deadline: float,
    *,
    recovery_owned: bool,
) -> None:
    """Terminal cleanup for a failed restart, retaining owners if it lags."""
    try:
        lsp_process_cleanup.drive_cleanup(
            instance,
            failure_deadline,
            terminal=True,
            failure_code="restart_failed",
            renew_deadline_after_evidence=recovery_owned,
        )
    finally:
        if recovery_owned and coordinator.cleanup_result.ownership_pending:
            retain_autonomous_cleanup_owners(instance)
