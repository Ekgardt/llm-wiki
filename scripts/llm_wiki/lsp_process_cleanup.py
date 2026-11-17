"""Driving the cleanup of an LSP child and of incomplete startups."""
from __future__ import annotations

import atexit
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import BinaryIO, NamedTuple

import lsp_process_tree as _lsp_process_tree
from interruption import (
    raise_collected_errors as _raise_cleanup_failures,
)
from llm_wiki import lsp_process_failure, lsp_process_launch, lsp_process_limits, lsp_process_state
from llm_wiki.lsp_process_failure import (
    ensure_failure_evidence,
    failure_evidence_required,
    join_owned_thread,
    linearize_terminal_outcome,
    linearize_terminal_outcome_locked,
    mark_terminal_failure,
    record_cleanup_error,
    remember_mandatory_terminal_failure,
    stop_recovery_owner,
    thread_never_started,
)
from llm_wiki.lsp_process_launch import (
    reject_protocol_callback_lifecycle,
)
from llm_wiki.lsp_process_limits import (
    PROCESS_EXITED,
    STARTUP_FAILED,
)
from llm_wiki.lsp_process_owner import (
    OwnerDirectory,
    validated_deadline,
)
from llm_wiki.lsp_process_state import (
    CleanupResult,
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    StderrWake,
    acquire_driver,
    acquire_lifecycle,
    coordinator_has_ownership,
    coordinator_has_ownership_locked,
    generations_locked,
    mark_generation_expected_exit,
    notify_lifecycle_locked,
    pending_startup_cleanup_snapshot,
    release_driver,
    release_lease,
    unregister_startup_cleanup,
)


class StartupCleanupError(RuntimeError):
    """Startup failed and owned cleanup remains incomplete or failed."""

    def __init__(
        self,
        message: str,
        *,
        coordinator: LifecycleCoordinator | None = None,
    ) -> None:
        super().__init__(message)
        self.coordinator = coordinator

    def retry_cleanup(self, deadline: float) -> None:
        """Retry retained startup ownership within one caller deadline."""
        coordinator = self.coordinator
        if coordinator is None:
            return
        retry_startup_cleanup(coordinator, deadline)

    def transfer_cleanup_ownership(self) -> None:
        """Transfer retry responsibility from the module registry to a caller."""
        coordinator = self.coordinator
        if coordinator is not None:
            unregister_startup_cleanup(coordinator)


def retry_startup_cleanup(
    coordinator: LifecycleCoordinator,
    deadline: float,
) -> None:
    deadline = validated_deadline(deadline)
    mark_terminal_failure(None, coordinator, STARTUP_FAILED, deadline)
    errors = drive_cleanup(
        None,
        deadline,
        terminal=True,
        failure_code=STARTUP_FAILED,
        coordinator_override=coordinator,
    )
    if errors:
        _raise_cleanup_failures(errors)
    if coordinator_has_ownership(coordinator):
        raise TimeoutError("LSP startup cleanup retains retryable ownership")
    unregister_startup_cleanup(coordinator)


def atexit_cleanup_startups() -> None:
    deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    for coordinator in pending_startup_cleanup_snapshot():
        try:
            retry_startup_cleanup(coordinator, deadline)
        except BaseException:
            pass


def terminal_failure_lsp_process(
    instance: LspProcessHandle,
    code: str,
    deadline: float,
) -> None:
    coordinator = instance._coordinator
    deadline = validated_deadline(deadline)
    reject_protocol_callback_lifecycle(instance)
    remember_mandatory_terminal_failure(instance, coordinator, code)
    acquire_driver(coordinator, deadline)
    try:
        mark_terminal_failure(instance, coordinator, code, deadline)
        errors = drive_cleanup(
            instance,
            deadline,
            terminal=True,
            failure_code=code,
        )
        if errors:
            _raise_cleanup_failures(errors)
    finally:
        release_driver(coordinator)
@dataclass
class _CleanupRun:
    """What every cleanup step shares: whose, how long, and where to record."""

    instance: LspProcessHandle | None
    coordinator: LifecycleCoordinator
    deadline: float
    terminal: bool
    failure_code: str | None
    result: CleanupResult
    errors: list[BaseException]

    def failed(self, step: str, error: BaseException) -> None:
        record_cleanup_error(self.result, self.errors, step, error)


class _CleanupStart(NamedTuple):
    """What the locked opening pass settled for the rest of the cleanup."""

    generations: list[Generation]
    outcome: str | None
    code: str
    terminal_outcome_ready: bool


def _cleanup_failure_code(
    coordinator: LifecycleCoordinator, failure_code: str | None
) -> str:
    """The code terminal failure evidence will carry."""
    return coordinator.terminal_code or failure_code or PROCESS_EXITED


def _try_failure_evidence(run: _CleanupRun, code: str) -> None:
    """Write terminal failure evidence; a refusal is recorded against the step."""
    try:
        ensure_failure_evidence(run.instance, run.coordinator, code, run.deadline)
    except BaseException as error:
        run.failed("evidence", error)


def _cleanup_outcome_locked(run: _CleanupRun) -> tuple[str | None, bool]:
    """The outcome cleanup acts on, and whether reading it settled anything."""
    coordinator = run.coordinator
    if not run.terminal:
        return coordinator.terminal_outcome, True
    try:
        outcome = linearize_terminal_outcome_locked(
            run.instance, coordinator, run.deadline, commit_success=False
        )
    except BaseException as error:
        run.failed("recovery_join", error)
        return coordinator.terminal_outcome, False
    return outcome, True


def _begin_cleanup_locked(run: _CleanupRun) -> _CleanupStart:
    coordinator = run.coordinator
    generations = generations_locked(coordinator)
    outcome, outcome_ready = _cleanup_outcome_locked(run)
    code = _cleanup_failure_code(coordinator, run.failure_code)
    if run.terminal:
        for generation in generations:
            mark_generation_expected_exit(generation)
    notify_lifecycle_locked(coordinator)
    return _CleanupStart(generations, outcome, code, outcome_ready)


def _open_cleanup(run: _CleanupRun) -> _CleanupStart | None:
    """The locked opening pass, or None when the lifecycle lock is out of reach."""
    coordinator = run.coordinator
    try:
        acquire_lifecycle(coordinator, run.deadline, allow_expired=True)
    except BaseException as error:
        run.failed("generation_joins", error)
        run.result.ownership_pending = True
        return None
    try:
        return _begin_cleanup_locked(run)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _evidence_unavailable(run: _CleanupRun, start: _CleanupStart) -> bool:
    """A failure with no owner directory able to hold its evidence."""
    if not run.terminal or start.outcome != "failure":
        return False
    owner = run.coordinator.owner_directory
    if owner is None:
        return True
    return not start.generations and not owner.owner_permissions_verified


def record_initial_evidence(run: _CleanupRun, start: _CleanupStart) -> None:
    """Write failure evidence up front, or mark the step as not applicable."""
    if not run.terminal:
        return
    if start.outcome != "failure" or _evidence_unavailable(run, start):
        run.result.succeeded("evidence", "not_applicable")
        return
    _try_failure_evidence(run, start.code)


class _TerminalCleanup(NamedTuple):
    """What the terminal pass settled about recovery and the outcome."""

    recovery_stopped: bool
    outcome: str | None
    outcome_ready: bool


def _recovery_can_take_over(recovery: threading.Thread | None) -> bool:
    """A live recovery thread other than the one asking."""
    if recovery is None or recovery is threading.current_thread():
        return False
    return recovery.is_alive()


def _autonomous_handoff(run: _CleanupRun, *, all_released: bool) -> bool:
    """Work is still owed, and a live recovery thread can finish it alone."""
    coordinator = run.coordinator
    if not coordinator.recovery_request_pending.is_set():
        return False
    if not _recovery_can_take_over(coordinator.recovery_thread):
        return False
    return bool(run.errors) or not all_released


def _record_recovery_join(result: CleanupResult, stopped: bool) -> None:
    if stopped:
        result.succeeded("recovery_join")
        return
    result.recovery_join = "pending"


def _stop_recovery_for_cleanup(run: _CleanupRun, *, all_released: bool) -> bool:
    """Stop the recovery owner, or leave the rest to a live one."""
    coordinator = run.coordinator
    if _autonomous_handoff(run, all_released=all_released):
        run.result.recovery_join = "pending"
        coordinator.recovery_wake.set()
        return False
    try:
        stopped = stop_recovery_owner(coordinator, run.deadline, allow_expired=True)
    except BaseException as error:
        run.failed("recovery_join", error)
        return False
    _record_recovery_join(run.result, stopped)
    return stopped


def _drain_terminal_failures_for_cleanup(run: _CleanupRun) -> None:
    try:
        lsp_process_failure.drain_terminal_failures(run.instance, run.coordinator, run.deadline)
    except BaseException as error:
        run.failed("recovery_join", error)


def _relinearize_outcome(
    run: _CleanupRun, *, commit_success: bool, step: str
) -> tuple[str | None, bool]:
    """The outcome after another linearization, and whether it settled."""
    try:
        outcome = linearize_terminal_outcome(
            run.instance, run.coordinator, run.deadline, commit_success=commit_success
        )
    except BaseException as error:
        run.failed(step, error)
        return run.coordinator.terminal_outcome, False
    return outcome, True


def _required_failure_evidence(run: _CleanupRun, outcome: str | None) -> None:
    """Write failure evidence when this outcome still demands it."""
    if outcome != "failure" or not failure_evidence_required(run.coordinator):
        return
    _try_failure_evidence(run, _cleanup_failure_code(run.coordinator, run.failure_code))


def _cleanup_evidence_ready(run: _CleanupRun, outcome: str | None) -> bool:
    """Nothing further is owed to failure evidence."""
    if outcome != "failure":
        return True
    if not failure_evidence_required(run.coordinator):
        return True
    return run.result.evidence == "success"


def _stop_heartbeat_for_cleanup(run: _CleanupRun) -> None:
    try:
        lsp_process_failure.stop_heartbeat_owner(run.coordinator, run.deadline, allow_expired=True)
        run.result.succeeded("heartbeat_join")
    except BaseException as error:
        run.failed("heartbeat_join", error)


def _commit_terminal_success(
    run: _CleanupRun, outcome: str | None
) -> tuple[str | None, bool]:
    """Commit the outcome, then release the heartbeat once evidence is in."""
    committed, ready = _relinearize_outcome(
        run, commit_success=True, step="heartbeat_join"
    )
    if not ready:
        return outcome, False
    _required_failure_evidence(run, committed)
    if _cleanup_evidence_ready(run, committed):
        _stop_heartbeat_for_cleanup(run)
    return committed, True


def _run_terminal_cleanup(run: _CleanupRun, *, all_released: bool) -> _TerminalCleanup:
    """Stop recovery, settle the terminal outcome, and commit what is owed."""
    recovery_stopped = _stop_recovery_for_cleanup(run, all_released=all_released)
    if recovery_stopped:
        _drain_terminal_failures_for_cleanup(run)
    outcome, ready = _relinearize_outcome(
        run, commit_success=False, step="recovery_join"
    )
    _required_failure_evidence(run, outcome)
    committable = all_released and recovery_stopped
    if committable and _cleanup_evidence_ready(run, outcome):
        outcome, ready = _commit_terminal_success(run, outcome)
    return _TerminalCleanup(recovery_stopped, outcome, ready)


def _drain_and_commit(run: _CleanupRun) -> tuple[str | None, bool]:
    """Drain terminal failures and commit the outcome as one step."""
    try:
        lsp_process_failure.drain_terminal_failures(run.instance, run.coordinator, run.deadline)
        outcome = linearize_terminal_outcome(
            run.instance, run.coordinator, run.deadline, commit_success=True
        )
    except BaseException as error:
        run.failed("recovery_join", error)
        return run.coordinator.terminal_outcome, False
    return outcome, True


def _commit_stopped_ownership(run: _CleanupRun) -> tuple[str | None, bool]:
    """Settle the outcome once nothing of ours is still running."""
    outcome, ready = _drain_and_commit(run)
    _required_failure_evidence(run, outcome)
    return outcome, ready


def _remove_owner_lease(run: _CleanupRun, owner: OwnerDirectory) -> None:
    coordinator = run.coordinator
    try:
        lsp_process_state.acquire_lease(coordinator, run.deadline, allow_expired=True)
        try:
            owner.remove_lease()
        finally:
            release_lease(coordinator)
        run.result.succeeded("lease_removal")
    except BaseException as error:
        run.failed("lease_removal", error)


def _remove_success_scratch(run: _CleanupRun, owner: OwnerDirectory) -> None:
    """Only a successful run's scratch goes; a failed run's scratch is evidence."""
    if run.coordinator.terminal_outcome != "success":
        run.result.succeeded("scratch_removal", "not_applicable")
        return
    try:
        owner.remove_success_scratch()
        run.result.succeeded("scratch_removal")
    except BaseException as error:
        run.failed("scratch_removal", error)


def _close_owner_handles(run: _CleanupRun, owner: OwnerDirectory) -> None:
    try:
        owner.close()
        run.result.succeeded("owner_handles")
    except BaseException as error:
        run.failed("owner_handles", error)


def _release_owner_directory(run: _CleanupRun, owner: OwnerDirectory) -> None:
    """Give up the owner directory: the lease, then the scratch, then the handles."""
    if not _cleanup_evidence_ready(run, run.coordinator.terminal_outcome):
        return
    _remove_owner_lease(run, owner)
    if run.result.lease_removal != "success":
        return
    _release_scratch_then_handles(run, owner)


def _release_scratch_then_handles(run: _CleanupRun, owner: OwnerDirectory) -> None:
    _remove_success_scratch(run, owner)
    if run.result.scratch_removal not in {"success", "not_applicable"}:
        return
    _close_owner_handles(run, owner)


def _forget_released_generations_locked(coordinator: LifecycleCoordinator) -> None:
    """Drop retired and candidate generations with nothing left to release."""
    coordinator.retired = [
        generation for generation in coordinator.retired if not generation.released
    ]
    candidate = coordinator.candidate
    if candidate is not None and candidate.released:
        coordinator.candidate = None


def _forget_closed_owner_locked(coordinator: LifecycleCoordinator) -> None:
    owner = coordinator.owner_directory
    if owner is not None and owner._closed:
        coordinator.owner_directory = None


def _forget_lease_generation_locked(coordinator: LifecycleCoordinator) -> None:
    if coordinator.owner_directory is None:
        coordinator.lease_generation = None


def _forget_active_generation_locked(
    coordinator: LifecycleCoordinator, *, terminal: bool
) -> None:
    if not terminal or coordinator.owner_directory is not None:
        return
    active = coordinator.active
    if active is not None and active.released:
        coordinator.active = None


def _terminal_outcome_settled(run: _CleanupRun, success_committed: bool) -> bool:
    """The recorded outcome already has everything it needs on disk."""
    outcome = run.coordinator.terminal_outcome
    if outcome == "failure":
        return run.result.evidence == "success"
    if outcome == "success":
        return success_committed
    return True


def _terminal_ownership_pending(run: _CleanupRun, *, outcome_ready: bool) -> bool:
    """Whether terminal ownership still owes something before it can be let go."""
    coordinator = run.coordinator
    with coordinator.terminal_state_lock:
        success_committed = coordinator.success_committed
        mandatory_failure_pending = (
            coordinator.mandatory_failure_intent is not None
            and coordinator.terminal_outcome != "failure"
        )
    if coordinator_has_ownership_locked(coordinator) or not outcome_ready:
        return True
    if mandatory_failure_pending:
        return True
    return not _terminal_outcome_settled(run, success_committed)


def _settle_ownership_pending_locked(run: _CleanupRun, *, outcome_ready: bool) -> None:
    """Record whether ownership is still owed, and clear a satisfied request."""
    coordinator = run.coordinator
    if not run.terminal:
        run.result.ownership_pending = any(
            not generation.released
            for generation in generations_locked(coordinator)
        )
        return
    run.result.ownership_pending = _terminal_ownership_pending(
        run, outcome_ready=outcome_ready
    )
    if run.result.ownership_pending:
        return
    coordinator.recovery_request_nonce = None
    coordinator.recovery_request_pending.clear()


def _terminal_phase(
    coordinator: LifecycleCoordinator, *, pending: bool
) -> LifecyclePhase:
    if pending:
        return LifecyclePhase.CLEANUP_PENDING
    if coordinator.terminal_outcome == "failure":
        return LifecyclePhase.STOPPED_FAILURE
    return LifecyclePhase.STOPPED_SUCCESS


def _settle_phase_locked(run: _CleanupRun) -> None:
    pending = run.result.ownership_pending
    if run.terminal:
        run.coordinator.phase = _terminal_phase(run.coordinator, pending=pending)
        return
    if pending:
        run.coordinator.phase = LifecyclePhase.CLEANUP_PENDING


def _close_cleanup_locked(run: _CleanupRun, *, outcome_ready: bool) -> None:
    """The locked closing pass: forget what is released, then record the phase."""
    coordinator = run.coordinator
    _forget_released_generations_locked(coordinator)
    _forget_closed_owner_locked(coordinator)
    _forget_lease_generation_locked(coordinator)
    _forget_active_generation_locked(coordinator, terminal=run.terminal)
    _settle_ownership_pending_locked(run, outcome_ready=outcome_ready)
    _settle_phase_locked(run)
    notify_lifecycle_locked(coordinator)


def _close_cleanup(run: _CleanupRun, *, outcome_ready: bool) -> bool:
    """Run the closing pass; False when the lifecycle lock stayed out of reach."""
    try:
        acquire_lifecycle(run.coordinator, run.deadline, allow_expired=True)
    except BaseException as error:
        run.failed("generation_joins", error)
        run.result.ownership_pending = True
        return False
    try:
        _close_cleanup_locked(run, outcome_ready=outcome_ready)
    finally:
        lsp_process_state.release_lifecycle(run.coordinator)
    return True


def _finish_cleanup_registration(run: _CleanupRun) -> None:
    """Stop holding the exit hooks once terminal ownership is fully released."""
    if not run.terminal or run.result.ownership_pending:
        return
    if run.instance is not None:
        atexit.unregister(run.instance._atexit_close)
    unregister_startup_cleanup(run.coordinator)


def _ownership_stopped(
    coordinator: LifecycleCoordinator, *, all_released: bool
) -> bool:
    """Nothing of ours is still running on this owner's behalf."""
    heartbeat = coordinator.heartbeat_thread
    recovery = coordinator.recovery_thread
    return (
        all_released
        and (heartbeat is None or not heartbeat.is_alive())
        and (recovery is None or not recovery.is_alive())
    )


def _finish_stopped_ownership(run: _CleanupRun) -> bool:
    """Commit the outcome and let the owner directory go; True when it settled."""
    owner = run.coordinator.owner_directory
    _, outcome_ready = _commit_stopped_ownership(run)
    if outcome_ready and owner is not None:
        _release_owner_directory(run, owner)
    return outcome_ready


def _settle_terminal_cleanup(run: _CleanupRun, start: _CleanupStart) -> bool:
    """Settle the terminal outcome; True when it no longer owes anything."""
    all_released = all(generation.released for generation in start.generations)
    if not run.terminal:
        return start.terminal_outcome_ready
    outcome_ready = _run_terminal_cleanup(run, all_released=all_released).outcome_ready
    if not _ownership_stopped(run.coordinator, all_released=all_released):
        return outcome_ready
    return _finish_stopped_ownership(run)


@dataclass
class _GenerationCleanup:
    """Which cleanup steps still hold, and which are merely not finished yet."""

    tree_termination_ok: bool = True
    protocol_stop_ok: bool = True
    protocol_stop_pending: bool = False
    tree_release_ok: bool = True
    tree_release_pending: bool = False
    joins_ok: bool = True
    joins_pending: bool = False


_COLLECTIVE_CLEANUP_STEPS = (
    ("tree_termination", "tree_termination_ok", None),
    ("protocol_stop", "protocol_stop_ok", "protocol_stop_pending"),
    ("tree_release", "tree_release_ok", "tree_release_pending"),
    ("generation_joins", "joins_ok", "joins_pending"),
)


def _stop_generation_protocol_io(
    generation: Generation,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    protocol = generation.protocol
    if protocol is None:
        return
    try:
        protocol._stop_io_for_process_cleanup()
    except BaseException as error:
        flags.protocol_stop_ok = False
        record_cleanup_error(result, errors, "protocol_stop", error)


def _terminate_generation_tree(
    generation: Generation,
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    tree = generation.tree
    if tree is None:
        return
    try:
        tree.terminate(deadline=deadline)
    except BaseException as error:
        flags.tree_termination_ok = False
        record_cleanup_error(result, errors, "tree_termination", error)


def _generation_process_exited(
    generation: Generation,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> bool:
    """Whether the server is gone; a process we cannot read counts as alive."""
    process = generation.process
    try:
        return process is None or process.poll() is not None
    except BaseException as error:
        flags.tree_termination_ok = False
        record_cleanup_error(result, errors, "tree_termination", error)
        return False


def _finish_generation_protocol(
    generation: Generation,
    process_exited: bool,
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    protocol = generation.protocol
    if protocol is None:
        return
    if not process_exited:
        flags.protocol_stop_pending = True
        return
    try:
        protocol._finish_io_after_process_exit(deadline)
    except BaseException as error:
        flags.protocol_stop_ok = False
        record_cleanup_error(result, errors, "protocol_stop", error)
        return
    generation.protocol = None


def _close_one_pipe(
    stream: BinaryIO | None,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> bool:
    if stream is None or getattr(stream, "closed", False):
        return True
    try:
        stream.close()
    except BaseException as error:
        flags.protocol_stop_ok = False
        record_cleanup_error(result, errors, "protocol_stop", error)
        return False
    return True


def stop_stderr_drain(generation: Generation, deadline: float) -> bool:
    """Join the stderr owner, preserving any uncertainty about its pipe closes."""
    thread = generation.stderr_thread
    wake = generation.stderr_wake
    if thread is None or thread_never_started(thread):
        _abandon_stderr_wake(wake)
        return True
    _request_stderr_wake(wake)
    return _joined_stderr_drain(thread, wake, deadline)


def _request_stderr_wake(wake: StderrWake | None) -> None:
    if wake is not None:
        wake.request()


def _joined_stderr_drain(thread, wake: StderrWake | None, deadline: float) -> bool:
    if not join_owned_thread(thread, deadline):
        return False
    _require_known_stderr_closes(wake)
    return True


def _require_known_stderr_closes(wake: StderrWake | None) -> None:
    if wake is not None:
        wake.require_known_closes()


def _abandon_stderr_wake(wake: StderrWake | None) -> None:
    if wake is not None:
        wake.abandon()


def _close_generation_pipes(
    generation: Generation,
    process_exited: bool,
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> bool:
    """Close the server's pipes once it is gone; False when one is still held."""
    process = generation.process
    if process is None or not process_exited:
        return True
    drained = stop_stderr_drain(generation, deadline)
    outcomes = [
        _close_one_pipe(stream, result, errors, flags)
        for stream in _closable_pipes(process, drained)
    ]
    return drained and all(outcomes)


def _closable_pipes(process: object, drained: bool) -> list[BinaryIO | None]:
    """Every pipe of ours; stderr only once its drain thread no longer holds it."""
    streams = [process.stdin, process.stdout]
    if drained:
        streams.append(process.stderr)
    return streams


def _release_posix_tree(
    generation: Generation,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    tree = generation.tree
    if tree is None or os.name == "nt":
        return
    try:
        tree.close()
    except BaseException as error:
        flags.tree_release_ok = False
        record_cleanup_error(result, errors, "tree_release", error)
        return
    generation.tree = None


def _join_one_generation_thread(
    generation: Generation,
    attribute: str,
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    thread = getattr(generation, attribute)
    if join_owned_thread(thread, deadline):
        setattr(generation, attribute, None)
        return
    flags.joins_ok = False
    error = TimeoutError("LSP generation thread did not stop before deadline")
    record_cleanup_error(result, errors, "generation_joins", error)


def _join_generation_threads(
    generation: Generation,
    process_exited: bool,
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    if not process_exited:
        flags.joins_pending = True
        return
    for attribute in ("stderr_thread", "exit_thread"):
        _join_one_generation_thread(
            generation, attribute, deadline, result, errors, flags
        )


def _windows_release_ready(generation: Generation, process_exited: bool) -> bool:
    """Nothing of ours still holds the process, so the job object can go."""
    return (
        process_exited
        and generation.protocol is None
        and generation.stderr_thread is None
        and generation.exit_thread is None
    )


def _release_windows_tree(
    generation: Generation,
    ready: bool,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    tree = generation.tree
    if tree is None or os.name != "nt":
        return
    if not ready:
        flags.tree_release_pending = True
        return
    try:
        tree.close()
    except BaseException as error:
        flags.tree_release_ok = False
        record_cleanup_error(result, errors, "tree_release", error)
        return
    generation.tree = None


def _release_windows_job(
    generation: Generation,
    ready: bool,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    windows_job = generation.windows_job
    if windows_job is None:
        return
    if not ready:
        flags.tree_release_pending = True
        return
    try:
        _lsp_process_tree.close_windows_handle(windows_job)
    except BaseException as error:
        flags.tree_release_ok = False
        record_cleanup_error(result, errors, "tree_release", error)
        return
    generation.windows_job = None


def _generation_fully_released(
    generation: Generation, process_exited: bool, pipes_closed: bool
) -> bool:
    handles = (
        generation.tree,
        generation.protocol,
        generation.stderr_thread,
        generation.exit_thread,
    )
    return (
        process_exited
        and pipes_closed
        and all(handle is None for handle in handles)
    )


def _forget_released_process(
    generation: Generation, process_exited: bool, pipes_closed: bool
) -> None:
    """Drop the process handle once nothing of ours refers to it any more."""
    if generation.process is None:
        return
    if _generation_fully_released(generation, process_exited, pipes_closed):
        generation.process = None


def _release_one_generation(
    generation: Generation,
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
    flags: _GenerationCleanup,
) -> None:
    """Every cleanup step for one generation, in the order they depend on."""
    _stop_generation_protocol_io(generation, result, errors, flags)
    _terminate_generation_tree(generation, deadline, result, errors, flags)
    exited = _generation_process_exited(generation, result, errors, flags)
    _finish_generation_protocol(generation, exited, deadline, result, errors, flags)
    pipes_closed = _close_generation_pipes(
        generation, exited, deadline, result, errors, flags
    )
    _release_posix_tree(generation, result, errors, flags)
    _join_generation_threads(generation, exited, deadline, result, errors, flags)
    ready = _windows_release_ready(generation, exited)
    _release_windows_tree(generation, ready, result, errors, flags)
    _release_windows_job(generation, ready, result, errors, flags)
    _forget_released_process(generation, exited, pipes_closed)


def _collective_step_done(
    flags: _GenerationCleanup, ok_field: str, pending_field: str | None
) -> bool:
    """Nothing failed, and nothing is still owed."""
    if not getattr(flags, ok_field):
        return False
    return pending_field is None or not getattr(flags, pending_field)


def _release_generations(
    generations: Sequence[Generation],
    deadline: float,
    result: CleanupResult,
    errors: list[BaseException],
) -> None:
    """Release every generation, then mark the steps that came out clean."""
    flags = _GenerationCleanup()
    for generation in generations:
        _release_one_generation(generation, deadline, result, errors, flags)
    for step, ok_field, pending_field in _COLLECTIVE_CLEANUP_STEPS:
        if _collective_step_done(flags, ok_field, pending_field):
            result.succeeded(step)




def drive_cleanup(
    instance: LspProcessHandle | None,
    deadline: float,
    *,
    terminal: bool,
    failure_code: str | None = None,
    coordinator_override: LifecycleCoordinator | None = None,
    renew_deadline_after_evidence: bool = False,
) -> list[BaseException]:
    coordinator = (
        instance._coordinator if instance is not None else coordinator_override
    )
    if coordinator is None:
        raise RuntimeError("LSP cleanup requires a lifecycle coordinator")
    if terminal:
        coordinator.cleanup_started.set()
    acquire_driver(coordinator, deadline, allow_expired=True)
    try:
        return _drive_cleanup_owned(
            instance,
            deadline,
            terminal=terminal,
            failure_code=failure_code,
            coordinator=coordinator,
            renew_deadline_after_evidence=renew_deadline_after_evidence,
        )
    finally:
        release_driver(coordinator)


def _drive_cleanup_owned(
    instance: LspProcessHandle | None,
    deadline: float,
    *,
    terminal: bool,
    failure_code: str | None,
    coordinator: LifecycleCoordinator,
    renew_deadline_after_evidence: bool,
) -> list[BaseException]:
    run = _CleanupRun(
        instance=instance,
        coordinator=coordinator,
        deadline=deadline,
        terminal=terminal,
        failure_code=failure_code,
        result=coordinator.cleanup_result,
        errors=[],
    )
    start = _open_cleanup(run)
    if start is None:
        return run.errors
    _drive_opened_cleanup(run, start, renew_deadline_after_evidence)
    return run.errors


def _drive_opened_cleanup(run: _CleanupRun, start, renew_deadline_after_evidence: bool) -> None:
    record_initial_evidence(run, start)
    if renew_deadline_after_evidence:
        run.deadline = lsp_process_launch.fresh_cleanup_deadline()
    _release_generations(start.generations, run.deadline, run.result, run.errors)
    outcome_ready = _settle_terminal_cleanup(run, start)
    if _close_cleanup(run, outcome_ready=outcome_ready):
        _finish_cleanup_registration(run)


atexit.register(atexit_cleanup_startups)
