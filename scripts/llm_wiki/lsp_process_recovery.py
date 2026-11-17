"""Background recovery and lifecycle workers of an LSP child."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import NamedTuple

from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_failure,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_restart,
    lsp_process_state,
)
from llm_wiki.lsp_process_failure import (
    remember_mandatory_terminal_failure,
)
from llm_wiki.lsp_process_generation import (
    heartbeat_loop,
    next_drain_deadline,
    queue_expired_drain,
)
from llm_wiki.lsp_process_limits import (
    PROCESS_EXITED,
    RECOVERY_RETRY_CEILING_SECONDS,
)
from llm_wiki.lsp_process_restart import (
    retain_autonomous_cleanup_owners,
)
from llm_wiki.lsp_process_state import (
    FailureIntent,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    acknowledge_failure_intent,
    acquire_driver,
    acquire_lifecycle,
    generations_locked,
    mark_generation_expected_exit,
    notify_lifecycle_locked,
    release_driver,
    remember_background_restart_error,
    require_startup_deadline,
)


def _adopt_lifecycle_workers_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> tuple[threading.Thread, threading.Thread]:
    """The recovery and heartbeat threads, created if they do not exist yet."""
    if coordinator.recovery_thread is None:
        coordinator.recovery_thread = threading.Thread(
            target=_recovery_loop,
            args=(instance,),
            name=f"lsp-recovery-{instance.owner_nonce}",
            daemon=True,
        )
    if coordinator.heartbeat_thread is None:
        coordinator.heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            args=(instance,),
            name=f"lsp-heartbeat-{instance.owner_nonce}",
            daemon=True,
        )
    return coordinator.recovery_thread, coordinator.heartbeat_thread


def _start_unstarted_worker(
    worker: threading.Thread, label: str, deadline: float
) -> None:
    """Start a worker that has not run yet, checking the deadline either side."""
    if worker.ident is not None or worker in threading.enumerate():
        return
    require_startup_deadline(deadline, f"{label} thread preparation")
    worker.start()
    require_startup_deadline(deadline, f"{label} thread start")


def start_lifecycle_workers(
    instance: LspProcessHandle, deadline: float | None = None
) -> None:
    coordinator = instance._coordinator
    if deadline is None:
        deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    acquire_lifecycle(coordinator, deadline)
    try:
        recovery, heartbeat = _adopt_lifecycle_workers_locked(instance, coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    _start_unstarted_worker(recovery, "recovery", deadline)
    _start_unstarted_worker(heartbeat, "heartbeat", deadline)


def start_heartbeat_worker(instance: LspProcessHandle, deadline: float) -> None:
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.heartbeat_thread is None:
            coordinator.heartbeat_thread = threading.Thread(
                target=heartbeat_loop,
                args=(instance,),
                name=f"lsp-heartbeat-{instance.owner_nonce}",
                daemon=True,
            )
        heartbeat = coordinator.heartbeat_thread
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    assert heartbeat is not None
    if heartbeat.ident is None and heartbeat not in threading.enumerate():
        require_startup_deadline(deadline, "heartbeat thread preparation")
        heartbeat.start()
        require_startup_deadline(deadline, "heartbeat thread start")


def _recovery_thread_serving(coordinator: LifecycleCoordinator) -> bool:
    """A live recovery thread that has not been asked to stop."""
    recovery = coordinator.recovery_thread
    if recovery is None or not recovery.is_alive():
        return False
    return not coordinator.recovery_stop.is_set()


def _wake_serving_recovery(coordinator: LifecycleCoordinator) -> bool:
    """Wake the live recovery thread; False when there is none to wake."""
    if not _recovery_thread_serving(coordinator):
        return False
    coordinator.recovery_wake.set()
    return True


def _wake_serving_recovery_locked(
    coordinator: LifecycleCoordinator, deadline: float
) -> bool:
    """The same check again under the lifecycle lock."""
    acquire_lifecycle(coordinator, deadline)
    try:
        return _wake_serving_recovery(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _stop_previous_recovery(
    coordinator: LifecycleCoordinator, deadline: float
) -> None:
    """Ask a recovery thread that is not us to finish before replacing it."""
    recovery = coordinator.recovery_thread
    if recovery is None or recovery is threading.current_thread():
        return
    coordinator.recovery_stop.set()
    coordinator.recovery_wake.set()
    remaining = deadline - time.monotonic()
    if remaining > 0:
        recovery.join(remaining)


def _adopt_recovery_thread_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> threading.Thread | None:
    """The replacement thread to start, or None when one is already serving."""
    if not coordinator.recovery_request_pending.is_set():
        return None
    current = coordinator.recovery_thread
    if current is not None and current.is_alive():
        coordinator.recovery_stop.clear()
        coordinator.cleanup_result.recovery_join = "pending"
        coordinator.recovery_wake.set()
        return None
    coordinator.recovery_stop.clear()
    coordinator.recovery_wake.clear()
    replacement = threading.Thread(
        target=_recovery_loop,
        args=(instance,),
        name=f"lsp-recovery-{instance.owner_nonce}",
        daemon=True,
    )
    coordinator.recovery_thread = replacement
    coordinator.cleanup_result.recovery_join = "pending"
    notify_lifecycle_locked(coordinator)
    return replacement


def _adopt_recovery_thread(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, deadline: float
) -> threading.Thread | None:
    acquire_lifecycle(coordinator, deadline)
    try:
        return _adopt_recovery_thread_locked(instance, coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def schedule_autonomous_recovery(instance: LspProcessHandle) -> None:
    coordinator = instance._coordinator
    if not coordinator.recovery_request_pending.is_set():
        return
    if _wake_serving_recovery(coordinator):
        return
    _start_replacement_recovery(
        instance, coordinator, time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    )


def _start_replacement_recovery(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, deadline: float
) -> None:
    if _wake_serving_recovery_locked(coordinator, deadline):
        return
    _stop_previous_recovery(coordinator, deadline)
    replacement = _adopt_recovery_thread(instance, coordinator, deadline)
    if replacement is None:
        return
    replacement.start()
    coordinator.recovery_wake.set()


@dataclass
class RecoveryState:
    """What the recovery loop still owes on its next pass."""

    request_retry: bool = False
    terminal_retry_code: str | None = None
    retry_seconds: float = lsp_process_limits.RECOVERY_RETRY_SECONDS

    @property
    def pending(self) -> bool:
        return self.request_retry or self.terminal_retry_code is not None

    def slow_down(self) -> None:
        """A pass that changed nothing earns a longer wait before the next.

        Cleanup that cannot finish — a descendant that will not die, a handle
        another process holds — used to be retried twenty times a second for
        the life of the process, each pass a `killpg` and an `fsync`.
        """
        self.retry_seconds = min(
            self.retry_seconds * 2, RECOVERY_RETRY_CEILING_SECONDS
        )

    def quicken(self) -> None:
        """Nothing is owed: the next retry starts from the short beat again."""
        self.retry_seconds = lsp_process_limits.RECOVERY_RETRY_SECONDS


def _recovery_wait(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    state: RecoveryState,
) -> None:
    """Wait for work: a backing-off retry beat, or the next drain deadline."""
    if state.pending:
        coordinator.recovery_wake.clear()
        if coordinator.recovery_stop.is_set():
            return
        coordinator.recovery_wake.wait(state.retry_seconds)
        state.slow_down()
        return
    state.quicken()
    drain_deadline = next_drain_deadline(instance)
    wait_for = (
        None
        if drain_deadline is None
        else max(0.0, drain_deadline - time.monotonic())
    )
    coordinator.recovery_wake.wait(wait_for)


def _retry_terminal_cleanup(
    instance: LspProcessHandle, state: RecoveryState
) -> bool:
    """True when the same terminal code still needs another pass."""
    if state.terminal_retry_code is None:
        return False
    if _retry_autonomous_terminal_cleanup(instance, state.terminal_retry_code):
        return True
    state.terminal_retry_code = None
    return False


def _serve_recovery_request(
    coordinator: LifecycleCoordinator,
    instance: LspProcessHandle,
    state: RecoveryState,
) -> None:
    """Run a pending maintenance request and record what it left owing."""
    if not coordinator.recovery_request_pending.is_set():
        state.request_retry = False
        return
    deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    state.request_retry, failure = process_recovery_request(instance, deadline)
    if failure is not None:
        state.terminal_retry_code = failure


def _drain_failure_intents(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    state: RecoveryState,
) -> None:
    """Take queued failure intents until one asks for a terminal retry."""
    queue_expired_drain(instance)
    while not coordinator.recovery_stop.is_set():
        try:
            intent = coordinator.failure_queue.get_nowait()
        except queue.Empty:
            return
        deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
        state.terminal_retry_code = process_failure_intent(
            instance, intent, deadline
        )
        if state.terminal_retry_code is not None:
            return


def _recovery_pass(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    state: RecoveryState,
) -> bool:
    """One pass of the recovery loop; False when the loop should stop."""
    _recovery_wait(instance, coordinator, state)
    coordinator.recovery_wake.clear()
    if coordinator.recovery_stop.is_set():
        return False
    if _retry_terminal_cleanup(instance, state):
        return True
    return _recovery_request_pass(instance, coordinator, state)


def _recovery_request_pass(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    state: RecoveryState,
) -> bool:
    if coordinator.recovery_stop.is_set():
        return False
    _serve_recovery_request(coordinator, instance, state)
    if state.pending:
        return True
    _drain_failure_intents(instance, coordinator, state)
    return True


def _recovery_loop(instance: LspProcessHandle) -> None:
    coordinator = instance._coordinator
    state = RecoveryState()
    while not coordinator.recovery_stop.is_set():
        if not _recovery_pass(instance, coordinator, state):
            return


def _acquire_recovery_driver(
    coordinator: LifecycleCoordinator,
    deadline: float,
) -> bool:
    while not coordinator.recovery_stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            acquire_driver(
                coordinator,
                min(deadline, time.monotonic() + min(0.05, remaining)),
            )
        except TimeoutError:
            continue
        return True
    return False


_RECOVERABLE_PHASES = frozenset(
    {
        LifecyclePhase.RECOVERY_PENDING,
        LifecyclePhase.RESTARTING,
        LifecyclePhase.CLEANUP_PENDING,
    }
)


def _clear_recovery_request_locked(coordinator: LifecycleCoordinator) -> None:
    """Nothing is owed to this request any more."""
    coordinator.recovery_request_nonce = None
    coordinator.recovery_request_pending.clear()
    notify_lifecycle_locked(coordinator)


def _terminal_recovery_outcome_locked(
    coordinator: LifecycleCoordinator,
) -> tuple[bool, str | None]:
    """What a lifecycle that already ended owes a recovery request."""
    if coordinator.cleanup_result.ownership_pending:
        return False, coordinator.terminal_code or "restart_failed"
    _clear_recovery_request_locked(coordinator)
    return False, None


def _requested_generation_owned_locked(
    coordinator: LifecycleCoordinator, requested_nonce: str
) -> bool:
    """Whether the generation this request names is still one of ours."""
    active = coordinator.active
    if active is not None and active.nonce == requested_nonce:
        return True
    return any(
        generation.nonce == requested_nonce
        for generation in generations_locked(coordinator)
    )


def _recovery_plan_locked(
    coordinator: LifecycleCoordinator,
) -> tuple[bool, str | None] | None:
    """What the request owes, or None when a restart should be attempted."""
    requested_nonce = coordinator.recovery_request_nonce
    if requested_nonce is None:
        coordinator.recovery_request_pending.clear()
        return False, None
    if coordinator.terminal_outcome is not None:
        return _terminal_recovery_outcome_locked(coordinator)
    return _owned_recovery_plan_locked(coordinator, requested_nonce)


def _owned_recovery_plan_locked(
    coordinator: LifecycleCoordinator, requested_nonce: str
) -> tuple[bool, str | None] | None:
    if not _requested_generation_owned_locked(coordinator, requested_nonce):
        _clear_recovery_request_locked(coordinator)
        return False, None
    if coordinator.phase not in _RECOVERABLE_PHASES:
        return True, None
    return None


def _recovery_plan(
    coordinator: LifecycleCoordinator, deadline: float
) -> tuple[bool, str | None] | None:
    """The plan under the lifecycle lock, or a retry when it is out of reach."""
    try:
        acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        return True, None
    try:
        return _recovery_plan_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _restart_for_recovery(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> tuple[bool, str | None]:
    """Restart the generation; a failure becomes a mandatory terminal failure."""
    try:
        lsp_process_restart.restart_generation(instance, lsp_process_launch.fresh_bootstrap_deadline(coordinator))
    except BaseException as restart_error:
        remember_background_restart_error(coordinator, restart_error)
        remember_mandatory_terminal_failure(
            instance,
            coordinator,
            "restart_failed",
        )
        if coordinator.cleanup_result.ownership_pending:
            retain_autonomous_cleanup_owners(instance)
            return False, "restart_failed"
    return False, None


def _recovery_driver_busy(
    coordinator: LifecycleCoordinator,
) -> tuple[bool, str | None]:
    """Another driver holds recovery; retry while a request is still pending."""
    return (
        coordinator.recovery_request_pending.is_set()
        and not coordinator.recovery_stop.is_set(),
        None,
    )


def process_recovery_request(
    instance: LspProcessHandle,
    deadline: float,
) -> tuple[bool, str | None]:
    coordinator = instance._coordinator
    if not _acquire_recovery_driver(coordinator, deadline):
        return _recovery_driver_busy(coordinator)
    try:
        planned = _recovery_plan(coordinator, deadline)
        if planned is not None:
            return planned
        return _restart_for_recovery(instance, coordinator)
    finally:
        release_driver(coordinator)


def _retry_autonomous_terminal_cleanup(
    instance: LspProcessHandle,
    code: str,
) -> bool:
    coordinator = instance._coordinator
    deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    if not _acquire_recovery_driver(coordinator, deadline):
        return _recovery_driver_busy(coordinator)[0]
    try:
        _try_terminal_failure(instance, coordinator, code, deadline)
        if not coordinator.cleanup_result.ownership_pending:
            return False
        return retain_autonomous_cleanup_owners(instance)
    finally:
        release_driver(coordinator)


def _try_terminal_failure(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    code: str,
    deadline: float,
) -> None:
    """Drive the terminal failure; a refusal becomes a background error."""
    try:
        lsp_process_cleanup.terminal_failure_lsp_process(instance, code, deadline)
    except BaseException as cleanup_error:
        lsp_process_state.remember_background_cleanup_error(coordinator, cleanup_error)


def _claim_driver_for_intent(
    coordinator: LifecycleCoordinator,
    intent: FailureIntent,
    deadline: float,
) -> bool:
    """Take the driver in short attempts; False when the intent goes back."""
    while True:
        if coordinator.recovery_stop.is_set():
            coordinator.failure_queue.put(intent)
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _requeue_intent(coordinator, intent)
            return False
        try:
            acquire_driver(
                coordinator,
                min(deadline, time.monotonic() + min(0.05, remaining)),
            )
        except TimeoutError:
            continue
        return True


def _restart_after_intent(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> str | None:
    """Restart the generation the intent retired; the code when it could not."""
    _, code = _restart_for_recovery(instance, coordinator)
    return code


def process_failure_intent(
    instance: LspProcessHandle,
    intent: FailureIntent,
    deadline: float,
) -> str | None:
    coordinator = instance._coordinator
    if not _claim_driver_for_intent(coordinator, intent, deadline):
        return None
    try:
        result, restart = _process_failure_intent_owned(instance, intent, deadline)
        if not restart:
            return result
        return _restart_after_intent(instance, coordinator)
    finally:
        release_driver(coordinator)


class _IntentDecision(NamedTuple):
    """What the locked pass decided about one failure intent."""

    terminal: bool
    restart: bool


def _requeue_intent(coordinator: LifecycleCoordinator, intent: FailureIntent) -> None:
    """Put the intent back and wake recovery to look at it again."""
    coordinator.failure_queue.put(intent)
    coordinator.recovery_wake.set()


def _intent_is_stale_locked(
    coordinator: LifecycleCoordinator, intent: FailureIntent
) -> bool:
    """A non-fatal intent for a generation that is no longer the active one."""
    if intent.owner_fatal:
        return False
    active = coordinator.active
    return active is None or active.nonce != intent.generation_nonce


def _intent_exhausted(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, intent: FailureIntent
) -> bool:
    """No restart is left for this failure."""
    return (
        intent.owner_fatal
        or coordinator.recovery_attempted
        or instance.restart_count >= 1
    )


def _terminal_intent_decision_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    intent: FailureIntent,
) -> _IntentDecision:
    """An exhausted intent: select the terminal failure, and stop when it is ours."""
    code = "heartbeat_failed" if intent.owner_fatal else PROCESS_EXITED
    terminal = lsp_process_failure.select_terminal_failure_locked(instance, coordinator, code)
    if terminal:
        coordinator.phase = LifecyclePhase.STOPPING_FAILURE
    return _IntentDecision(terminal, False)


def _decide_failure_intent_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    intent: FailureIntent,
) -> _IntentDecision:
    """Whether this failure ends the lifecycle or asks for one restart."""
    if _intent_exhausted(instance, coordinator, intent):
        return _terminal_intent_decision_locked(instance, coordinator, intent)
    coordinator.recovery_attempted = True
    coordinator.phase = LifecyclePhase.RECOVERY_PENDING
    instance.state = ProcessState.DEGRADED
    active = coordinator.active
    if active is not None:
        mark_generation_expected_exit(active)
    return _IntentDecision(False, True)


def _claim_failure_intent_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    intent: FailureIntent,
    key: tuple[str, bool],
) -> tuple[_IntentDecision | None, bool]:
    """The decision to drive, and whether the intent should be acknowledged."""
    if key in coordinator.seen_failures:
        return None, True
    if not coordinator.startup_complete:
        _requeue_intent(coordinator, intent)
        return None, False
    return _decide_unseen_intent_locked(instance, coordinator, intent, key)


def _decide_unseen_intent_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    intent: FailureIntent,
    key: tuple[str, bool],
) -> tuple[_IntentDecision | None, bool]:
    stale = _intent_is_stale_locked(coordinator, intent)
    coordinator.seen_failures.add(key)
    if stale:
        return None, True
    decision = _decide_failure_intent_locked(instance, coordinator, intent)
    notify_lifecycle_locked(coordinator)
    return decision, True


def _settle_failure_intent(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    intent: FailureIntent,
    key: tuple[str, bool],
) -> _IntentDecision | None:
    """Decide the intent under the lifecycle lock the caller holds, then release it."""
    acknowledge = False
    try:
        decision, acknowledge = _claim_failure_intent_locked(
            instance, coordinator, intent, key
        )
        return decision
    finally:
        lsp_process_state.release_lifecycle(coordinator)
        if acknowledge:
            acknowledge_failure_intent(coordinator)


def _terminal_cleanup_code(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, deadline: float
) -> str | None:
    """The code to report when terminal cleanup left ownership pending."""
    _drive_terminal_cleanup(instance, coordinator, deadline)
    if coordinator.cleanup_result.ownership_pending:
        return coordinator.terminal_code or PROCESS_EXITED
    return None


def _drive_terminal_cleanup(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator, deadline: float
) -> None:
    """Drive terminal cleanup, remembering the first error it raised or reported."""
    try:
        errors = lsp_process_cleanup.drive_cleanup(
            instance,
            deadline,
            terminal=True,
            failure_code=coordinator.terminal_code,
        )
    except BaseException as cleanup_error:
        lsp_process_state.remember_background_cleanup_error(coordinator, cleanup_error)
        return
    if errors:
        lsp_process_state.remember_background_cleanup_error(coordinator, errors[0])


def _process_failure_intent_owned(
    instance: LspProcessHandle,
    intent: FailureIntent,
    deadline: float,
) -> tuple[str | None, bool]:
    coordinator = instance._coordinator
    key = (intent.generation_nonce, intent.owner_fatal)
    try:
        acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        _requeue_intent(coordinator, intent)
        return None, False
    decision = _settle_failure_intent(instance, coordinator, intent, key)
    if decision is None:
        return None, False
    return _failure_intent_outcome(instance, coordinator, decision, deadline)


def _failure_intent_outcome(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    decision: _IntentDecision,
    deadline: float,
) -> tuple[str | None, bool]:
    """A terminal decision's cleanup code, or whether the settled intent restarts."""
    if decision.terminal:
        code = _terminal_cleanup_code(instance, coordinator, deadline)
        if code is not None:
            return code, False
    return None, decision.restart
