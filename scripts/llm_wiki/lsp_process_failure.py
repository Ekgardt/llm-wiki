"""Terminal failures of an LSP child, their intents, evidence and selection."""
from __future__ import annotations

import json
import queue
import threading
import time

from llm_wiki import lsp_process_state
from llm_wiki.lsp_process_limits import (
    PROCESS_EXITED,
    STDERR_REDACTION_WINDOW_BYTES,
    STDERR_TAIL_BYTES,
)
from llm_wiki.lsp_process_owner import (
    OwnerDirectory,
)
from llm_wiki.lsp_process_state import (
    CleanupResult,
    FailureEvidenceIdentity,
    FailureIntent,
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    acknowledge_failure_intent,
    acquire_lifecycle,
    acquire_terminal_intent,
    generations_locked,
    mark_generation_expected_exit,
    notify_lifecycle_locked,
    release_terminal_intent,
    subprocess,
    validate_failure_record,
)
from lsp_security import redact_lsp_text, redact_private_key_blocks


def _failure_generation(
    coordinator: LifecycleCoordinator,
) -> Generation | None:
    """The generation the failure evidence should name."""
    generation = coordinator.active or coordinator.candidate
    if generation is not None:
        return generation
    if coordinator.retired:
        return coordinator.retired[-1]
    return None


def _failure_generation_nonce(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    generation: Generation | None,
) -> str | None:
    """The generation nonce, from the generation, the instance, or the startup."""
    if generation is not None:
        return generation.nonce
    if instance is not None:
        return instance.generation_nonce
    return coordinator.startup_generation_nonce


def _failure_server_pid(
    instance: LspProcessHandle | None, generation: Generation | None
) -> int | None:
    """The server PID this failure belongs to, from the closest thing that knows."""
    if generation is None:
        return instance.process.pid if instance is not None else None
    if generation.process is not None:
        return generation.process.pid
    return generation.server_pid


def _failure_owner_nonce(
    instance: LspProcessHandle | None, owner: OwnerDirectory
) -> str:
    if instance is not None:
        return instance.owner_nonce
    return owner.owner_root.name


def _failure_identity(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    code: str,
) -> FailureEvidenceIdentity | None:
    owner = coordinator.owner_directory
    if owner is None:
        return None
    generation = _failure_generation(coordinator)
    generation_nonce = _failure_generation_nonce(instance, coordinator, generation)
    if generation_nonce is None:
        return None
    return FailureEvidenceIdentity(
        code,
        _failure_owner_nonce(instance, owner),
        generation_nonce,
        _failure_server_pid(instance, generation),
        failure_stderr_tail(generation),
    )


def failure_stderr_tail(generation: Generation | None) -> str | None:
    """The last words the failed server wrote, redacted, or None when it wrote none.

    This is the only thing a failed start leaves an operator to read, so it
    goes through `redact_lsp_text` before it is written and is bounded so the
    evidence record stays inside `_MAX_EVIDENCE_BYTES`.
    """
    if generation is None:
        return None
    with generation.stderr_lock:
        written = b"".join(generation.stderr)
    return _bounded_stderr_tail(_whole_line_window(written))


def _whole_line_window(written: bytes) -> bytes:
    """The last lines that fit the redaction window, none of them cut at its start.

    The tail was cut to its record bound first, so a `token=` just before the cut
    left its value to a redactor that needs the key (audit C-37,
    docs/research/2026-09-25-a-stderr-tail-is-redacted-before-it-is-cut.md).
    """
    if len(written) <= STDERR_REDACTION_WINDOW_BYTES:
        return written
    window = written[-STDERR_REDACTION_WINDOW_BYTES:]
    return window.partition(b"\n")[2]


def _redacted_lines(tail: bytes) -> str:
    """Every non-empty line, redacted on its own, after any private key block.

    A credential assignment runs to the end of its line, so the redactor is
    given one line at a time: one `authorization:` must not swallow every
    message the server printed after it. A key spans lines, so it goes first.
    """
    text = redact_private_key_blocks(tail.decode("utf-8", errors="replace"))
    return "\n".join(redact_lsp_text(line) for line in text.splitlines() if line)


def _bounded_stderr_tail(tail: bytes) -> str | None:
    """The tail as the record will carry it, halved until its encoding fits."""
    text = _redacted_lines(tail)
    while text and len(json.dumps(text)) > STDERR_TAIL_BYTES:
        text = text[len(text) // 2 :]
    return text or None


def remember_mandatory_terminal_failure(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    code: str,
) -> None:
    identity = _failure_identity(instance, coordinator, code)
    if identity is None:
        return
    with coordinator.terminal_state_lock:
        if (
            not coordinator.success_committed
            and coordinator.mandatory_failure_intent is None
        ):
            coordinator.mandatory_failure_intent = identity


def select_terminal_failure_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    code: str,
) -> bool:
    if coordinator.success_committed:
        return False
    coordinator.terminal_outcome = "failure"
    _fill_failure_identity_locked(instance, coordinator, code)
    _degrade_unfailed_instance(instance)
    return True


def _fill_failure_identity_locked(
    instance: LspProcessHandle | None, coordinator: LifecycleCoordinator, code: str
) -> None:
    """The first failure's code and evidence identity win; later ones keep them."""
    if coordinator.terminal_code is None:
        coordinator.terminal_code = code
    if coordinator.failure_evidence_identity is None:
        coordinator.failure_evidence_identity = _failure_identity(
            instance, coordinator, coordinator.terminal_code
        )


def _degrade_unfailed_instance(instance: LspProcessHandle | None) -> None:
    """An instance that has not already failed is at least degraded."""
    if instance is None or instance.state is ProcessState.FAILED:
        return
    instance.state = ProcessState.DEGRADED


def _mark_generations_expected_exit_locked(coordinator: LifecycleCoordinator) -> None:
    for generation in generations_locked(coordinator):
        mark_generation_expected_exit(generation)


def mark_terminal_failure(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    code: str,
    deadline: float,
) -> None:
    acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        if select_terminal_failure_locked(instance, coordinator, code):
            coordinator.phase = LifecyclePhase.STOPPING_FAILURE
            _mark_generations_expected_exit_locked(coordinator)
            notify_lifecycle_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def await_process_exit(process: object, deadline: float) -> int:
    """The exit code, refusing to wait past the deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 and process.poll() is None:
        raise TimeoutError("LSP process did not exit before deadline")
    try:
        return process.wait(timeout=max(0.0, remaining))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("LSP process did not exit before deadline") from exc


def await_stream_owner(owner: threading.Thread | None, deadline: float) -> None:
    """Wait out one stream owner; our own thread is already accounted for."""
    if owner is None or owner is threading.current_thread():
        return
    _join_until(owner, deadline)
    if owner.is_alive():
        raise TimeoutError("LSP process streams did not drain before deadline")


def _join_until(thread: threading.Thread, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        thread.join(remaining)


def thread_never_started(thread: threading.Thread) -> bool:
    """A thread object that was built but never handed to the scheduler."""
    return thread.ident is None and thread not in threading.enumerate()


def join_owned_thread(thread: threading.Thread | None, deadline: float) -> bool:
    if thread is None:
        return True
    if thread is threading.current_thread():
        return False
    return _joined(thread, deadline)


def _joined(thread: threading.Thread, deadline: float) -> bool:
    if thread_never_started(thread):
        return True
    _join_until(thread, deadline)
    return not thread.is_alive()


def record_cleanup_error(
    result: CleanupResult,
    current: list[BaseException],
    step: str,
    error: BaseException,
) -> None:
    result.failed(step, error)
    current.append(error)


def _failure_evidence_owner_locked(
    coordinator: LifecycleCoordinator, code: str
) -> OwnerDirectory:
    """The owner directory this evidence belongs in, for exactly this code."""
    owner = coordinator.owner_directory
    if owner is None:
        raise RuntimeError("LSP failure evidence owner is unavailable")
    terminal_code = coordinator.terminal_code
    if terminal_code is None or code != terminal_code:
        raise RuntimeError(
            "LSP failure evidence code does not match terminal identity"
        )
    return owner


def _settled_failure_identity_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    terminal_code: str,
) -> FailureEvidenceIdentity:
    identity = coordinator.failure_evidence_identity
    if identity is not None:
        return identity
    identity = _failure_identity(instance, coordinator, terminal_code)
    if identity is None:
        raise RuntimeError(
            "LSP failure evidence generation identity is unavailable"
        )
    coordinator.failure_evidence_identity = identity
    return identity


def _failure_evidence_identity_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    terminal_code: str,
) -> FailureEvidenceIdentity:
    """The identity this evidence is written under, settled once and kept."""
    identity = _settled_failure_identity_locked(instance, coordinator, terminal_code)
    if identity.code != terminal_code:
        raise RuntimeError(
            "LSP failure evidence identity is not terminal-code exact"
        )
    return identity


def _claim_failure_evidence(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    code: str,
    deadline: float,
) -> tuple[OwnerDirectory, FailureEvidenceIdentity]:
    acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        owner = _failure_evidence_owner_locked(coordinator, code)
        identity = _failure_evidence_identity_locked(instance, coordinator, code)
        return owner, identity
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _write_failure_evidence_once(
    owner: OwnerDirectory, identity: FailureEvidenceIdentity
) -> None:
    """Write the record, accepting one that is already there and identical."""
    try:
        lsp_process_state.write_failure_record(
            owner,
            code=identity.code,
            owner_nonce=identity.owner_nonce,
            generation_nonce=identity.generation_nonce,
            pid=identity.pid,
            stderr_tail=identity.stderr_tail,
        )
    except FileExistsError:
        validate_failure_record(
            owner.read_record("failure.json"),
            code=identity.code,
            owner_nonce=identity.owner_nonce,
            generation_nonce=identity.generation_nonce,
            pid=identity.pid,
            stderr_tail=identity.stderr_tail,
        )
        owner.sync_directory()


def _commit_failure_evidence(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    identity: FailureEvidenceIdentity,
    deadline: float,
) -> None:
    """Record the evidence as written, unless the identity moved meanwhile."""
    acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        if (
            coordinator.terminal_code != identity.code
            or coordinator.failure_evidence_identity != identity
        ):
            raise RuntimeError(
                "LSP terminal identity changed during evidence write"
            )
        coordinator.cleanup_result.succeeded("evidence")
        if instance is not None:
            instance.state = ProcessState.FAILED
        notify_lifecycle_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def ensure_failure_evidence(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    code: str,
    deadline: float,
) -> None:
    owner, identity = _claim_failure_evidence(instance, coordinator, code, deadline)
    with owner._child_handle_lock:
        owner._retry_pending_temp_names()
        if coordinator.cleanup_result.evidence == "success":
            return
        _write_failure_evidence_once(owner, identity)
        owner._retry_pending_temp_names()
        _commit_failure_evidence(instance, coordinator, identity, deadline)


def failure_evidence_required(coordinator: LifecycleCoordinator) -> bool:
    owner = coordinator.owner_directory
    return owner is not None and owner.owner_permissions_verified


def stop_heartbeat_owner(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        thread = coordinator.heartbeat_thread
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    coordinator.heartbeat_stop.set()
    coordinator.heartbeat_wake.set()
    if not join_owned_thread(thread, deadline):
        raise TimeoutError("LSP heartbeat thread did not stop before deadline")
    acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        if coordinator.heartbeat_thread is thread:
            coordinator.heartbeat_thread = None
            notify_lifecycle_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def stop_recovery_owner(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> bool:
    thread = _current_recovery_thread(
        coordinator, deadline, allow_expired=allow_expired
    )
    coordinator.recovery_stop.set()
    coordinator.recovery_wake.set()
    if thread is not threading.current_thread() and not join_owned_thread(
        thread, deadline
    ):
        raise TimeoutError("LSP recovery thread did not stop before deadline")
    _forget_recovery_thread(coordinator, thread, deadline, allow_expired=allow_expired)
    return True


def _current_recovery_thread(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool,
) -> threading.Thread | None:
    acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        return coordinator.recovery_thread
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _forget_recovery_thread(
    coordinator: LifecycleCoordinator,
    thread: threading.Thread | None,
    deadline: float,
    *,
    allow_expired: bool,
) -> None:
    """Drop the recovery thread, if it is still the one we stopped."""
    acquire_lifecycle(coordinator, deadline, allow_expired=allow_expired)
    try:
        if coordinator.recovery_thread is thread:
            coordinator.recovery_thread = None
            notify_lifecycle_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _take_queued_intents(
    coordinator: LifecycleCoordinator,
) -> list[FailureIntent]:
    """Everything the failure queue holds right now."""
    intents: list[FailureIntent] = []
    while True:
        try:
            intents.append(coordinator.failure_queue.get_nowait())
        except queue.Empty:
            return intents


def _return_queued_intents(
    coordinator: LifecycleCoordinator, intents: list[FailureIntent]
) -> None:
    """Put the intents back and wake recovery to take them again."""
    for intent in intents:
        coordinator.failure_queue.put(intent)
    coordinator.recovery_wake.set()


def _drained_failure_code(intents: list[FailureIntent]) -> str:
    """One code for the whole batch; an owner-fatal intent outranks an exit."""
    if any(intent.owner_fatal for intent in intents):
        return "heartbeat_failed"
    return PROCESS_EXITED


def _select_drained_failure_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    intents: list[FailureIntent],
) -> None:
    selected = select_terminal_failure_locked(
        instance, coordinator, _drained_failure_code(intents)
    )
    if selected:
        coordinator.phase = LifecyclePhase.STOPPING_FAILURE
        notify_lifecycle_locked(coordinator)


def drain_terminal_failures(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    deadline: float,
) -> None:
    intents = _take_queued_intents(coordinator)
    if not intents:
        return
    try:
        acquire_lifecycle(coordinator, deadline, allow_expired=True)
    except BaseException:
        _return_queued_intents(coordinator, intents)
        raise
    try:
        _select_drained_failure_locked(instance, coordinator, intents)
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    for _intent in intents:
        acknowledge_failure_intent(coordinator)


def _commit_mandatory_failure_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    mandatory_intent: FailureEvidenceIdentity,
) -> None:
    """A mandatory failure names the terminal outcome and its evidence."""
    coordinator.terminal_outcome = "failure"
    coordinator.terminal_code = mandatory_intent.code
    coordinator.failure_evidence_identity = mandatory_intent
    if instance is not None and instance.state is not ProcessState.FAILED:
        instance.state = ProcessState.DEGRADED
    coordinator.phase = LifecyclePhase.STOPPING_FAILURE


def _settle_terminal_outcome_locked(
    instance: LspProcessHandle | None, coordinator: LifecycleCoordinator
) -> bool:
    """Fix the terminal outcome; True when generations must be marked exiting."""
    mandatory_intent = coordinator.mandatory_failure_intent
    if mandatory_intent is not None:
        _commit_mandatory_failure_locked(instance, coordinator, mandatory_intent)
        return True
    if coordinator.pending_failure_intents <= 0:
        return False
    select_terminal_failure_locked(
        instance,
        coordinator,
        coordinator.pending_failure_code or PROCESS_EXITED,
    )
    coordinator.phase = LifecyclePhase.STOPPING_FAILURE
    return True


def _commit_success_locked(
    coordinator: LifecycleCoordinator, *, commit_success: bool
) -> None:
    """Only an unopposed success may be committed."""
    if not commit_success or coordinator.mandatory_failure_intent is not None:
        return
    if coordinator.terminal_outcome == "success":
        coordinator.success_committed = True


def _linearize_terminal_state_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    *,
    commit_success: bool,
) -> tuple[str | None, bool]:
    """The outcome, and whether the generations still need marking."""
    with coordinator.terminal_state_lock:
        if coordinator.success_committed:
            return "success", False
        mark_failure_exits = _settle_terminal_outcome_locked(instance, coordinator)
        _commit_success_locked(coordinator, commit_success=commit_success)
    return coordinator.terminal_outcome, mark_failure_exits


def linearize_terminal_outcome_locked(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    commit_success: bool,
) -> str | None:
    acquire_terminal_intent(
        coordinator,
        deadline,
        allow_expired=not commit_success,
    )
    try:
        outcome, mark_failure_exits = _linearize_terminal_state_locked(
            instance, coordinator, commit_success=commit_success
        )
        if mark_failure_exits:
            _mark_generations_expected_exit_locked(coordinator)
        return outcome
    finally:
        release_terminal_intent(coordinator)


def linearize_terminal_outcome(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    commit_success: bool,
) -> str | None:
    acquire_lifecycle(coordinator, deadline, allow_expired=True)
    try:
        outcome = linearize_terminal_outcome_locked(
            instance, coordinator, deadline, commit_success=commit_success
        )
        notify_lifecycle_locked(coordinator)
        return outcome
    finally:
        lsp_process_state.release_lifecycle(coordinator)
