"""Requests and notifications sent through the active generation of an LSP child."""
from __future__ import annotations

import re
import time
from typing import NamedTuple

from llm_wiki import lsp_process_state
from llm_wiki.lsp_process_limits import (
    PROCESS_EXITED,
)
from llm_wiki.lsp_process_owner import (
    validated_deadline,
)
from llm_wiki.lsp_process_state import (
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    acquire_lifecycle,
    notify_lifecycle_locked,
    take_background_restart_error,
)
from lsp_protocol import (
    CancellationToken,
    LocalRequestViolation,
    LspProtocol,
    ProtocolViolation,
)

_TRANSITIONING_PHASES = frozenset(
    {
        LifecyclePhase.STARTING,
        LifecyclePhase.RECOVERY_PENDING,
        LifecyclePhase.RESTARTING,
    }
)

_FINISHING_PHASES = frozenset(
    {
        LifecyclePhase.STOPPING_SUCCESS,
        LifecyclePhase.STOPPING_FAILURE,
        LifecyclePhase.CLEANUP_PENDING,
        LifecyclePhase.STOPPED_SUCCESS,
        LifecyclePhase.STOPPED_FAILURE,
    }
)


def _await_stable_phase_locked(
    coordinator: LifecycleCoordinator, deadline: float
) -> None:
    """Wait out a transition; the caller holds the lifecycle lock."""
    while coordinator.phase in _TRANSITIONING_PHASES:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not coordinator.condition.wait(remaining):
            raise TimeoutError(
                "LSP lifecycle transition did not finish before deadline"
            )


def _require_serving_lifecycle_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> None:
    """A lifecycle that is ending cannot serve a request."""
    if not _lifecycle_finished(coordinator):
        return
    if instance.state is ProcessState.FAILED:
        raise RuntimeError("LSP process has exited")
    raise RuntimeError("LSP process is closed")


class _GenerationChannel(NamedTuple):
    """One generation and the two ends of its channel, taken together.

    Cleanup drops `generation.protocol` and `generation.process` on another
    thread. Reading them after the lifecycle lock is released could therefore
    see None on a generation that had both when it was chosen. Holding our own
    references instead means a retire that lands now ends in the protocol's own
    `ProtocolViolation` — the retry path — not in a broken invariant.
    """

    generation: Generation
    protocol: LspProtocol
    process: object


def request_generation(instance: LspProcessHandle, deadline: float) -> _GenerationChannel:
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        _await_stable_phase_locked(coordinator, deadline)
        _require_serving_lifecycle_locked(instance, coordinator)
        generation = coordinator.active
        if generation is None or not generation_has_channel(generation):
            raise RuntimeError("LSP process generation is unavailable")
        instance.last_used_monotonic = time.monotonic()
        return _GenerationChannel(generation, generation.protocol, generation.process)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _promotion_lifecycle_ready(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation_nonce: str,
) -> bool:
    """The lifecycle is running, unfinished, and on this exact generation."""
    if coordinator.phase is not LifecyclePhase.RUNNING:
        return False
    if coordinator.terminal_outcome is not None:
        return False
    return instance.generation_nonce == generation_nonce


def _promotable_generation_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation_nonce: str,
) -> Generation | None:
    """The live generation this promotion names, or None when it is not it."""
    if not _promotion_lifecycle_ready(instance, coordinator, generation_nonce):
        return None
    generation = coordinator.active
    if generation is None or generation.nonce != generation_nonce:
        return None
    return generation if generation_has_channel(generation) else None


def generation_has_channel(generation: Generation) -> bool:
    """The generation still holds both ends of its channel."""
    return generation.process is not None and generation.protocol is not None


def _generation_still_healthy(generation: Generation) -> bool:
    """Nothing has told us this generation is finishing; caller holds its lock."""
    if generation._exit_observed or generation.failure_queued:
        return False
    if generation.expected_exit.is_set():
        return False
    return generation.process.poll() is None and not generation.protocol.fatal


def _terminal_outcome_uncommitted_locked(coordinator: LifecycleCoordinator) -> bool:
    """No terminal outcome is committed or owed; caller holds the state lock."""
    if coordinator.success_committed:
        return False
    if coordinator.mandatory_failure_intent is not None:
        return False
    return coordinator.pending_failure_intents == 0


def _promote_generation_locked(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation: Generation,
) -> bool:
    """Mark the process workspace-ready, unless something is already ending it."""
    with generation.failure_lock:
        if not _generation_still_healthy(generation):
            return False
        if not _mark_workspace_ready_unless_terminal(instance, coordinator):
            return False
    notify_lifecycle_locked(coordinator)
    return True


def _mark_workspace_ready_unless_terminal(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> bool:
    with coordinator.terminal_state_lock:
        if not _terminal_outcome_uncommitted_locked(coordinator):
            return False
        instance.state = ProcessState.WORKSPACE_READY
        return True


def promote_lsp_process_workspace_ready(
    instance: LspProcessHandle,
    *,
    generation_nonce: str,
    deadline: float,
) -> bool:
    deadline = validated_deadline(deadline)
    _require_generation_nonce(generation_nonce)

    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        generation = _promotable_generation_locked(
            instance, coordinator, generation_nonce
        )
        if generation is None:
            return False
        return _promote_generation_locked(instance, coordinator, generation)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def wait_for_generation_change(
    instance: LspProcessHandle,
    generation_nonce: str,
    deadline: float,
) -> tuple[bool, str | None]:
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        return _await_generation_change_locked(coordinator, generation_nonce, deadline)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _await_generation_change_locked(
    coordinator: LifecycleCoordinator, generation_nonce: str, deadline: float
) -> tuple[bool, str | None]:
    while True:
        settled = _generation_change_outcome_locked(coordinator, generation_nonce)
        if settled is not None:
            return settled
        _await_lifecycle_change(coordinator, deadline)


def _await_lifecycle_change(
    coordinator: LifecycleCoordinator, deadline: float
) -> None:
    """Wait for a lifecycle notification; the caller holds the lock."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not coordinator.condition.wait(remaining):
        raise TimeoutError("LSP recovery did not finish before deadline")


def _lifecycle_finished(coordinator: LifecycleCoordinator) -> bool:
    """The lifecycle has an outcome, or is on its way to resting."""
    return (
        coordinator.terminal_outcome is not None
        or coordinator.phase in _FINISHING_PHASES
    )


def _generation_change_outcome_locked(
    coordinator: LifecycleCoordinator, generation_nonce: str
) -> tuple[bool, str | None] | None:
    """Whether the generation changed, or None while it is still the same one."""
    if _lifecycle_finished(coordinator):
        return False, coordinator.terminal_code
    return _running_generation_change(coordinator, generation_nonce)


def _running_generation_change(
    coordinator: LifecycleCoordinator, generation_nonce: str
) -> tuple[bool, str | None] | None:
    active = coordinator.active
    if coordinator.phase is not LifecyclePhase.RUNNING or active is None:
        return None
    if active.nonce != generation_nonce:
        return True, None
    return None


def _generation_changed(
    instance: LspProcessHandle,
    generation: Generation,
    deadline: float,
) -> bool:
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        return coordinator.active is not generation
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _raise_replacement_startup_failure(
    coordinator: LifecycleCoordinator,
) -> None:
    cause = take_background_restart_error(coordinator)
    if cause is not None:
        raise ProtocolViolation("LSP replacement startup failed") from cause
    raise ProtocolViolation("LSP replacement startup failed") from None


_RETRY_REQUEST = object()


def _retry_or_fail(
    instance: LspProcessHandle,
    generation: Generation,
    deadline: float,
    *,
    attempt: int,
) -> bool:
    """True to try a fresh generation; otherwise the caller's error stands."""
    changed, terminal_code = wait_for_generation_change(
        instance, generation.nonce, deadline
    )
    if attempt == 0 and changed:
        return True
    if terminal_code == "restart_failed":
        _raise_replacement_startup_failure(instance._coordinator)
    return False


def _generation_unusable(protocol: LspProtocol, process: object) -> str | None:
    """Why this generation cannot serve a request, if it cannot."""
    if protocol.expired_drain_keys(time.monotonic()):
        return "expired drain"
    if process.poll() is not None or protocol.fatal:
        return PROCESS_EXITED
    return None


def _refuse_unusable_generation(
    instance: LspProcessHandle,
    generation: Generation,
    unusable: str,
    deadline: float,
    attempt: int,
) -> object:
    """Record the failure, then either ask for a retry or refuse the request."""
    lsp_process_state.queue_generation_failure(instance._coordinator, generation, unusable)
    if _retry_or_fail(instance, generation, deadline, attempt=attempt):
        return _RETRY_REQUEST
    raise ProtocolViolation("LSP generation is fatally unavailable")


def _objectively_fatal(
    instance: LspProcessHandle,
    generation: Generation,
    protocol: LspProtocol,
    process: object,
    deadline: float,
) -> bool:
    """The violation reflects a dead generation, not a bad request."""
    if _generation_changed(instance, generation, deadline):
        return True
    return protocol.fatal or process.poll() is not None


def _serve_lsp_request(
    instance: LspProcessHandle,
    channel: _GenerationChannel,
    method: str,
    params: object,
    *,
    deadline: float,
    cancellation: CancellationToken | None,
    attempt: int,
) -> object:
    """Send the request on this generation, retrying only a fatal one."""
    generation, protocol, process = channel
    try:
        return protocol.request(
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,
        )
    except LocalRequestViolation:
        raise
    except ProtocolViolation:
        if not _objectively_fatal(
            instance, generation, protocol, process, deadline
        ):
            raise
        lsp_process_state.queue_generation_failure(
            instance._coordinator, generation, PROCESS_EXITED
        )
        if _retry_or_fail(instance, generation, deadline, attempt=attempt):
            return _RETRY_REQUEST
        raise


def _attempt_lsp_request(
    instance: LspProcessHandle,
    method: str,
    params: object,
    *,
    deadline: float,
    cancellation: CancellationToken | None,
    attempt: int,
) -> object:
    """One attempt; the retry sentinel means the generation moved under us."""
    channel = request_generation(instance, deadline)
    unusable = _generation_unusable(channel.protocol, channel.process)
    if unusable is not None:
        return _refuse_unusable_generation(
            instance, channel.generation, unusable, deadline, attempt
        )
    return _serve_lsp_request(
        instance,
        channel,
        method,
        params,
        deadline=deadline,
        cancellation=cancellation,
        attempt=attempt,
    )


def request_lsp_process(
    instance: LspProcessHandle,
    method: str,
    params: object,
    *,
    deadline: float,
    cancellation: CancellationToken | None,
) -> object:
    deadline = validated_deadline(deadline)
    for attempt in range(2):
        outcome = _attempt_lsp_request(
            instance,
            method,
            params,
            deadline=deadline,
            cancellation=cancellation,
            attempt=attempt,
        )
        if outcome is not _RETRY_REQUEST:
            return outcome
    raise RuntimeError("LSP request retry invariant breached")


def notify_lsp_process(
    instance: LspProcessHandle,
    method: str,
    params: object,
    *,
    deadline: float,
) -> None:
    deadline = validated_deadline(deadline)
    generation, protocol, process = request_generation(instance, deadline)
    unusable = _generation_unusable(protocol, process)
    if unusable is not None:
        lsp_process_state.queue_generation_failure(instance._coordinator, generation, unusable)
        raise ProtocolViolation("LSP generation is fatally unavailable")
    try:
        protocol.notify(method, params, deadline=deadline)
    except ProtocolViolation:
        if _generation_died_after_violation(generation):
            lsp_process_state.queue_generation_failure(
                instance._coordinator,
                generation,
                PROCESS_EXITED,
            )
        raise


def _require_generation_nonce(generation_nonce: object) -> None:
    if not isinstance(generation_nonce, str):
        raise TypeError("generation_nonce must be a string")
    if re.fullmatch(r"[0-9a-f]{32}", generation_nonce) is None:
        raise ValueError(
            "generation_nonce must be 32 lowercase hexadecimal characters"
        )


def _generation_still_current(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation: object,
    generation_nonce: str,
) -> bool:
    if coordinator.phase is not LifecyclePhase.RUNNING or coordinator.terminal_outcome is not None:
        return False
    if coordinator.active is not generation:
        return False
    return instance.generation_nonce == generation_nonce


def _generation_unavailable(generation: object) -> bool:
    with generation.failure_lock:
        if generation._exit_observed or generation.failure_queued:
            return True
        return generation.expected_exit.is_set()


def _terminal_pending(coordinator: LifecycleCoordinator) -> bool:
    with coordinator.terminal_state_lock:
        if coordinator.success_committed:
            return True
        if coordinator.mandatory_failure_intent is not None:
            return True
        return coordinator.pending_failure_intents > 0


class _NotifyOutcome(NamedTuple):
    sent: bool
    queue_reason: str | None
    protocol_error: ProtocolViolation | None


def _notify_under_lifecycle(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation: object,
    generation_nonce: str,
    method: str,
    params: object,
    deadline: float,
) -> _NotifyOutcome:
    if not _notifiable_generation(
        instance, coordinator, generation, generation_nonce
    ):
        return _NotifyOutcome(False, None, None)
    protocol = generation.protocol
    process = generation.process
    unusable = _generation_unusable(protocol, process)
    if unusable is not None:
        return _NotifyOutcome(False, unusable, None)
    try:
        protocol.notify(method, params, deadline=deadline)
    except ProtocolViolation as error:
        return _NotifyOutcome(False, None, error)
    return _NotifyOutcome(True, None, None)


def _notifiable_generation(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation: Generation,
    generation_nonce: str,
) -> bool:
    """The generation is still ours, still whole, and nothing is ending it."""
    if not _generation_still_current(
        instance, coordinator, generation, generation_nonce
    ):
        return False
    if not generation_has_channel(generation):
        return False
    return not _generation_unavailable(generation) and not _terminal_pending(
        coordinator
    )


def _generation_died_after_violation(generation: object) -> bool:
    protocol = generation.protocol
    process = generation.process
    if protocol is None or process is None:
        return False
    return protocol.fatal or process.poll() is not None


def notify_lsp_process_generation(
    instance: LspProcessHandle,
    method: str,
    params: object,
    *,
    generation_nonce: str,
    deadline: float,
) -> bool:
    deadline = validated_deadline(deadline)
    _require_generation_nonce(generation_nonce)
    generation = request_generation(instance, deadline).generation
    if generation.nonce != generation_nonce:
        return False
    coordinator = instance._coordinator
    acquire_lifecycle(coordinator, deadline)
    try:
        outcome = _notify_under_lifecycle(
            instance,
            coordinator,
            generation,
            generation_nonce,
            method,
            params,
            deadline,
        )
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    return _settle_notify_outcome(coordinator, generation, outcome)


def _settle_notify_outcome(
    coordinator: LifecycleCoordinator,
    generation: Generation,
    outcome: _NotifyOutcome,
) -> bool:
    """Record what the notification met, then report or raise it."""
    if outcome.queue_reason is not None:
        lsp_process_state.queue_generation_failure(coordinator, generation, outcome.queue_reason)
    if outcome.protocol_error is None:
        return outcome.sent
    _raise_protocol_violation(coordinator, generation, outcome.protocol_error)
    return False


def _raise_protocol_violation(
    coordinator: LifecycleCoordinator, generation: Generation, protocol_error: BaseException
) -> None:
    if _generation_died_after_violation(generation):
        lsp_process_state.queue_generation_failure(coordinator, generation, PROCESS_EXITED)
    raise protocol_error
