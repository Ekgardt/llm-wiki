"""Caller-driven shutdown, restart and exit waits of an LSP child."""
from __future__ import annotations

import threading
import time

from interruption import (
    interruption_in_chain as _interruption_in_chain,
)
from interruption import (
    raise_collected_errors as _raise_cleanup_failures,
)
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_restart,
    lsp_process_state,
)
from llm_wiki.lsp_process_failure import (
    await_process_exit,
    await_stream_owner,
    linearize_terminal_outcome_locked,
)
from llm_wiki.lsp_process_launch import (
    reject_protocol_callback_lifecycle,
)
from llm_wiki.lsp_process_limits import (
    PROCESS_EXITED,
)
from llm_wiki.lsp_process_owner import (
    validated_deadline,
)
from llm_wiki.lsp_process_recovery import (
    schedule_autonomous_recovery,
)
from llm_wiki.lsp_process_state import (
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    acquire_driver,
    acquire_lifecycle,
    coordinator_has_ownership_locked,
    mark_generation_expected_exit,
    notify_lifecycle_locked,
    release_driver,
    take_background_cleanup_error,
)
from lsp_protocol import (
    ProtocolViolation,
)


def _hand_off_to_recovery(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> None:
    """Ask recovery to pick up what a failed restart left behind."""
    try:
        schedule_autonomous_recovery(instance)
    except BaseException as recovery_error:
        recovery = coordinator.recovery_thread
        if recovery is None or not recovery.is_alive():
            lsp_process_state.remember_background_cleanup_error(coordinator, recovery_error)
        coordinator.recovery_wake.set()


def _finish_restart(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    *,
    driver_acquired: bool,
    handoff_required: bool,
) -> None:
    """Release the driver, then hand off if the restart did not complete."""
    try:
        if driver_acquired:
            release_driver(coordinator)
    finally:
        if handoff_required:
            _hand_off_to_recovery(instance, coordinator)


def restart_lsp_process(instance: LspProcessHandle, deadline: float) -> None:
    deadline = validated_deadline(deadline)
    coordinator = instance._coordinator
    reject_protocol_callback_lifecycle(instance)
    driver_acquired = False
    handoff_required = False
    try:
        acquire_driver(coordinator, deadline)
        driver_acquired = True
        _restart_lsp_process_owned(instance, deadline)
        lsp_process_restart.restart_generation(instance, deadline)
    except BaseException:
        handoff_required = True
        raise
    finally:
        _finish_restart(
            instance,
            coordinator,
            driver_acquired=driver_acquired,
            handoff_required=handoff_required,
        )


def _begin_caller_recovery_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> None:
    """Ask for one recovery of the active generation."""
    active = coordinator.active
    if active is None:
        raise RuntimeError("LSP process generation is unavailable")
    coordinator.recovery_attempted = True
    coordinator.recovery_request_nonce = active.nonce
    coordinator.recovery_request_pending.set()
    coordinator.phase = LifecyclePhase.RECOVERY_PENDING
    instance.state = ProcessState.DEGRADED
    mark_generation_expected_exit(active)
    notify_lifecycle_locked(coordinator)


def _claim_caller_restart_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> bool:
    """True when no restart is left and the lifecycle must end instead."""
    if coordinator.terminal_outcome is not None:
        raise RuntimeError("LSP process is closed")
    if coordinator.recovery_attempted or instance.restart_count >= 1:
        return True
    _begin_caller_recovery_locked(instance, coordinator)
    return False


def _restart_lsp_process_owned(instance: LspProcessHandle, deadline: float) -> None:
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        terminal = _claim_caller_restart_locked(instance, coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    if terminal:
        lsp_process_cleanup.terminal_failure_lsp_process(
            instance, PROCESS_EXITED, lsp_process_launch.fresh_cleanup_deadline()
        )
        raise ProtocolViolation("LSP process restart limit exceeded")


def shutdown_lsp_process(instance: LspProcessHandle, deadline: float) -> None:
    deadline = validated_deadline(deadline)
    coordinator = instance._coordinator
    reject_protocol_callback_lifecycle(instance)
    acquire_driver(coordinator, deadline)
    try:
        _shutdown_lsp_process_owned(instance, deadline)
    finally:
        release_driver(coordinator)


def _raise_background_cleanup_error(coordinator: LifecycleCoordinator) -> None:
    """Surface an error a background cleanup recorded, when there is one."""
    background_error = take_background_cleanup_error(coordinator)
    if background_error is not None:
        raise background_error


def _stopped_without_ownership_locked(coordinator: LifecycleCoordinator) -> bool:
    """A stopped lifecycle this caller no longer owns has nothing left to stop."""
    stopped = {LifecyclePhase.STOPPED_SUCCESS, LifecyclePhase.STOPPED_FAILURE}
    return coordinator.phase in stopped and not coordinator_has_ownership_locked(
        coordinator
    )


def _begin_shutdown_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    deadline: float,
) -> tuple[bool, Generation | None]:
    """Enter the stopping phase and report the failure flag and the generation."""
    if coordinator.terminal_outcome is None:
        coordinator.terminal_outcome = "success"
    linearize_terminal_outcome_locked(
        instance, coordinator, deadline, commit_success=False
    )
    failure = coordinator.terminal_outcome == "failure"
    coordinator.phase = (
        LifecyclePhase.STOPPING_FAILURE
        if failure
        else LifecyclePhase.STOPPING_SUCCESS
    )
    generation = coordinator.active
    if generation is not None:
        mark_generation_expected_exit(generation)
    notify_lifecycle_locked(coordinator)
    return failure, generation


def _enter_shutdown(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    deadline: float,
) -> tuple[bool, Generation | None] | None:
    """What to shut down, or None when the lifecycle has already stopped."""
    acquire_lifecycle(coordinator, deadline)
    try:
        if _stopped_without_ownership_locked(coordinator):
            _raise_background_cleanup_error(coordinator)
            return None
        return _begin_shutdown_locked(instance, coordinator, deadline)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _request_graceful_exit(
    instance: LspProcessHandle, generation: Generation, graceful_deadline: float
) -> None:
    """Ask a live server to shut itself down and wait for it to go."""
    process = generation.process
    protocol = generation.protocol
    if process is None or protocol is None:
        return
    if process.poll() is not None or time.monotonic() >= graceful_deadline:
        return
    protocol.request("shutdown", {}, deadline=graceful_deadline)
    protocol.notify("exit", {}, deadline=graceful_deadline)
    wait_for_lsp_exit(instance, graceful_deadline)


def _graceful_exit_error(
    instance: LspProcessHandle, generation: Generation, deadline: float
) -> BaseException | None:
    """The graceful-exit error worth reporting; an ordinary refusal is not one."""
    graceful_deadline = min(deadline, time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS)
    try:
        _request_graceful_exit(instance, generation, graceful_deadline)
    except (OSError, RuntimeError, TimeoutError) as error:
        if _interruption_in_chain(error) is not None:
            return error
        return None
    except BaseException as error:
        return error
    return None


def _shutdown_graceful_error(
    instance: LspProcessHandle,
    generation: Generation | None,
    deadline: float,
    *,
    failure: bool,
) -> BaseException | None:
    """Only a live generation ending well is asked to leave politely."""
    if failure or generation is None:
        return None
    return _graceful_exit_error(instance, generation, deadline)


def _finish_shutdown(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    deadline: float,
    graceful_error: BaseException | None,
) -> None:
    """Run terminal cleanup and raise whatever it or the background left behind."""
    errors = lsp_process_cleanup.drive_cleanup(
        instance,
        deadline,
        terminal=True,
        failure_code=coordinator.terminal_code,
    )
    if errors or graceful_error is not None:
        _raise_cleanup_failures(errors, prior_error=graceful_error)
    _raise_background_cleanup_error(coordinator)


def _shutdown_lsp_process_owned(instance: LspProcessHandle, deadline: float) -> None:
    coordinator = instance._coordinator
    entered = _enter_shutdown(instance, coordinator, deadline)
    if entered is None:
        return
    failure, generation = entered
    graceful_error = _shutdown_graceful_error(
        instance, generation, deadline, failure=failure
    )
    _finish_shutdown(instance, coordinator, deadline, graceful_error)


def cancel_all_lsp_process(instance: LspProcessHandle, reason: str) -> None:
    deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.terminal_outcome == "failure":
            return
        generation = coordinator.active
        if generation is None or generation.protocol is None:
            raise RuntimeError("LSP process is closed")
        protocol = generation.protocol
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    protocol.cancel_all(reason)


def _exit_stream_owners(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, deadline: float
) -> tuple[threading.Thread | None, threading.Thread | None]:
    """The stderr and exit threads of the projected generation."""
    acquire_lifecycle(coordinator, deadline)
    try:
        generation = instance._projected_generation()
        if generation is None:
            return None, None
        return generation.stderr_thread, generation.exit_thread
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def wait_for_lsp_exit(instance: LspProcessHandle, deadline: float) -> int:
    deadline = validated_deadline(deadline)
    coordinator = instance._coordinator
    stderr_thread, exit_thread = _exit_stream_owners(instance, coordinator, deadline)
    return_code = await_process_exit(instance.process, deadline)
    for owner in (stderr_thread, exit_thread):
        await_stream_owner(owner, deadline)
    return return_code
