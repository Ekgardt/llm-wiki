"""Preparing, leasing and heartbeating one generation of an LSP child."""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lsp_process_tree as _lsp_process_tree
from interruption import (
    raise_collected_errors as _raise_cleanup_failures,
)
from llm_wiki import lsp_process_failure, lsp_process_limits, lsp_process_state
from llm_wiki.lsp_process_failure import (
    join_owned_thread,
)
from llm_wiki.lsp_process_launch import (
    wrap_protocol_callback,
)
from llm_wiki.lsp_process_owner import (
    OwnerDirectory,
)
from llm_wiki.lsp_process_state import (
    Generation,
    GenerationConfiguration,
    GenerationLaunch,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    ProcessTree,
    acquire_lifecycle,
    assign_candidate,
    commit_generation_failure,
    drain_stderr,
    generations_locked,
    mark_generation_expected_exit,
    new_stderr_wake,
    notify_lifecycle_locked,
    protocol_startup_cleanup_in_chain,
    queue_owner_failure,
    release_lease,
    require_startup_deadline,
)
from llm_wiki.lsp_process_sweep import (
    add_start_identity,
)
from lsp_protocol import (
    LspProtocol,
)


def _spawn_generation_tree(
    generation: Generation,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    deadline: float,
    pass_fds: Sequence[int],
) -> ProcessTree:
    """Spawn the server tree, keeping whatever a failed spawn still owns."""
    spawn_options: dict[str, object] = {}
    if pass_fds:
        spawn_options["pass_fds"] = pass_fds
    try:
        return ProcessTree._spawn_with_deadline(
            command,
            cwd=cwd,
            env=environment,
            deadline=deadline,
            **spawn_options,
        )
    except _lsp_process_tree.ProcessTreeSpawnError as error:
        generation.tree = error.tree
        if error.tree is None:
            generation.windows_job = error.windows_job
        else:
            generation.process = error.tree.process
        raise


def _adopt_generation_tree(
    generation: Generation, tree: ProcessTree, deadline: float
) -> object:
    """Take the spawned tree, and check it gave us all three pipes."""
    generation.tree = tree
    generation.process = tree.process
    process = tree.process
    generation.server_pid = process.pid
    require_startup_deadline(deadline, "process-tree start")
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("LSP process pipes were not created")
    return process


def _start_stderr_drain(
    generation: Generation, process: object, generation_nonce: str, deadline: float
) -> None:
    generation.stderr_wake = new_stderr_wake()
    stderr_thread = threading.Thread(
        target=drain_stderr,
        args=(
            process.stderr,
            generation.stderr,
            generation.stderr_size,
            generation.stderr_lock,
            generation.stderr_wake,
        ),
        name=f"lsp-stderr-{generation_nonce}",
        daemon=True,
    )
    generation.stderr_thread = stderr_thread
    require_startup_deadline(deadline, "stderr thread preparation")
    stderr_thread.start()
    require_startup_deadline(deadline, "stderr thread start")


def _publish_owner_record(
    owner: OwnerDirectory,
    owner_record: Mapping[str, object] | None,
    process: object,
) -> None:
    """Record who owns this generation, naming the process that serves it."""
    if owner_record is None:
        return
    published_owner_record = dict(owner_record)
    published_owner_record["owner_pid"] = process.pid
    add_start_identity(published_owner_record, "owner_start_identity", process.pid)
    lsp_process_state.write_owner_record(owner, published_owner_record)
    owner.verify_lexical_identity()


def _generation_handler_options(
    coordinator: LifecycleCoordinator,
) -> dict[str, object]:
    """Protocol handlers, wrapped so a callback failure reaches the lifecycle."""
    configuration = coordinator.generation_configuration
    if configuration.generation_bootstrap is None:
        return {}
    return {
        "server_request_handlers": {
            method: wrap_protocol_callback(coordinator, handler)
            for method, handler in configuration.server_request_handlers.items()
        },
        "server_notification_handlers": {
            method: wrap_protocol_callback(coordinator, handler)
            for method, handler in configuration.server_notification_handlers.items()
        },
    }


def report_protocol_warning(message: str) -> None:
    """A protocol warning goes to stderr, the log an MCP stdio server may write."""
    print(f"llm-wiki lsp: {message}", file=sys.stderr)


def _start_generation_protocol(
    coordinator: LifecycleCoordinator,
    generation: Generation,
    process: object,
    generation_nonce: str,
    deadline: float,
) -> LspProtocol:
    """Start the protocol, keeping the owner a failed startup handed back."""
    try:
        protocol = LspProtocol(
            process.stdout,
            process.stdin,
            generation_nonce,
            fatal_callback=lambda reason: lsp_process_state.queue_generation_failure(
                coordinator, generation, reason
            ),
            warning_callback=report_protocol_warning,
            _startup_deadline=deadline,
            _drain_wake=coordinator.recovery_wake,
            **_generation_handler_options(coordinator),
        )  # type: ignore[arg-type]
    except BaseException as error:
        cleanup_owner = protocol_startup_cleanup_in_chain(error)
        if cleanup_owner is not None:
            generation.protocol = cleanup_owner.protocol
        raise
    generation.protocol = protocol
    require_startup_deadline(deadline, "protocol owner start")
    return protocol


def _start_exit_monitor(
    coordinator: LifecycleCoordinator,
    generation: Generation,
    generation_nonce: str,
    deadline: float,
) -> None:
    exit_thread = threading.Thread(
        target=_monitor_generation_exit,
        args=(coordinator, generation),
        name=f"lsp-exit-{generation_nonce}",
        daemon=True,
    )
    generation.exit_thread = exit_thread
    require_startup_deadline(deadline, "exit thread preparation")
    exit_thread.start()
    require_startup_deadline(deadline, "exit thread start")


def _require_owner_directory(coordinator: LifecycleCoordinator) -> OwnerDirectory:
    owner = coordinator.owner_directory
    if owner is None:
        raise RuntimeError("LSP owner directory is unavailable")
    return owner


def prepare_generation(
    coordinator: LifecycleCoordinator,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    deadline: float,
    generation_nonce: str,
    owner_record: Mapping[str, object] | None = None,
    pass_fds: Sequence[int] = (),
) -> Generation:
    generation = Generation(generation_nonce, None, None)
    assign_candidate(coordinator, generation, deadline)
    tree = _spawn_generation_tree(
        generation,
        command,
        cwd=cwd,
        environment=environment,
        deadline=deadline,
        pass_fds=pass_fds,
    )
    process = _adopt_generation_tree(generation, tree, deadline)
    _start_stderr_drain(generation, process, generation_nonce, deadline)
    owner = _require_owner_directory(coordinator)
    owner.verify_lexical_identity()
    _publish_owner_record(owner, owner_record, process)
    protocol = _start_generation_protocol(
        coordinator, generation, process, generation_nonce, deadline
    )
    owner.verify_lexical_identity()
    _start_exit_monitor(coordinator, generation, generation_nonce, deadline)
    if process.poll() is not None or protocol.fatal:
        raise RuntimeError("LSP process exited during generation startup")
    return generation


def _monitor_generation_exit(
    coordinator: LifecycleCoordinator,
    generation: Generation,
) -> None:
    process = generation.process
    if process is None:
        return
    try:
        process.wait()
    except BaseException as error:
        lsp_process_state.queue_generation_failure(coordinator, generation, str(error))
        return
    _await_last_words(generation)
    _fail_on_unexpected_exit(coordinator, generation)


def _await_last_words(generation: Generation) -> None:
    """Let the drain thread finish before the exit is reported.

    The server is gone, so its end of the pipe is closed and the thread is
    about to see end-of-file. Waiting here is what puts the server's last
    words in the failure evidence instead of a race. The bound is the one
    cleanup itself uses; a process that escaped the group and kept the write
    end open costs that much and no more, on a thread that holds no lock.
    """
    join_owned_thread(
        generation.stderr_thread, time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    )


def _fail_on_unexpected_exit(coordinator: LifecycleCoordinator, generation: Generation) -> None:
    if not commit_generation_failure(
        coordinator,
        generation,
        "LSP process exited unexpectedly",
        exit_observed=True,
    ):
        return
    protocol = generation.protocol
    if protocol is not None:
        protocol._become_fatal("LSP process exited unexpectedly")


def write_generation_lease(
    owner: OwnerDirectory,
    generation: Generation,
    owner_nonce: str,
    deadline: float,
    retry_stop: threading.Event,
) -> None:
    process = generation.process
    server_pid = process.pid if process is not None else generation.server_pid
    if server_pid is None:
        raise RuntimeError("LSP generation process is unavailable")
    heartbeat_monotonic = time.monotonic()
    heartbeat = datetime.now(UTC)
    expires = heartbeat + timedelta(seconds=lsp_process_limits.LEASE_EXPIRY_SECONDS)
    record: dict[str, object] = {
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "generation_nonce": generation.nonce,
            "heartbeat_at": heartbeat.isoformat().replace("+00:00", "Z"),
            "manager_pid": os.getpid(),
            "owner_nonce": owner_nonce,
            "schema_version": 1,
            "server_pid": server_pid,
            "state": "live",
        }
    add_start_identity(record, "manager_start_identity", os.getpid())
    add_start_identity(record, "server_start_identity", server_pid)
    owner.write_lease(
        record,
        deadline=deadline,
        expires_monotonic=heartbeat_monotonic + lsp_process_limits.LEASE_EXPIRY_SECONDS,
        retry_stop=retry_stop,
    )


def publish_generation_lease(
    instance: LspProcessHandle,
    generation: Generation,
    deadline: float,
) -> None:
    coordinator = instance._coordinator
    owner = coordinator.owner_directory
    if owner is None:
        raise RuntimeError("LSP owner directory is unavailable")
    lsp_process_state.acquire_lease(coordinator, deadline)
    try:
        _adopt_lease_generation(coordinator, generation, deadline)
        _publish_current_lease(instance, coordinator, owner, generation, deadline)
    finally:
        release_lease(coordinator)


def _adopt_lease_generation(
    coordinator: LifecycleCoordinator, generation: Generation, deadline: float
) -> None:
    """Point the lease at this candidate, if it is still the candidate."""
    acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.candidate is not generation:
            raise RuntimeError("LSP lease candidate lost lifecycle ownership")
        coordinator.lease_generation = generation
        notify_lifecycle_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _run_generation_bootstrap(
    configuration: GenerationConfiguration,
    generation: Generation,
    deadline: float,
) -> ProcessState:
    bootstrap = configuration.generation_bootstrap
    if bootstrap is None:
        return ProcessState.PROCESS_RUNNING
    process = generation.process
    protocol = generation.protocol
    if process is None or protocol is None:
        raise RuntimeError("LSP generation bootstrap owners are unavailable")
    require_startup_deadline(deadline, "generation bootstrap start")
    state = bootstrap(protocol, process.pid, generation.nonce, deadline)
    require_startup_deadline(deadline, "generation bootstrap completion")
    _check_bootstrap_state(state)
    # PROCESS_RUNNING explicitly represents a configured no-op bootstrap.
    return state


_ACTIVE_BOOTSTRAP_STATES = frozenset(
    {
        ProcessState.PROCESS_RUNNING,
        ProcessState.PROTOCOL_INITIALIZED,
        ProcessState.WORKSPACE_READY,
    }
)


def _check_bootstrap_state(state: object) -> None:
    """A bootstrap may only report a state an active generation can hold."""
    if not isinstance(state, ProcessState):
        raise TypeError("generation_bootstrap must return a ProcessState")
    if state not in _ACTIVE_BOOTSTRAP_STATES:
        raise ValueError("generation_bootstrap returned an invalid active state")


@contextmanager
def generation_guard_context(
    configuration: GenerationConfiguration,
    generation_nonce: str,
    deadline: float,
) -> Iterator[GenerationLaunch | None]:
    factory = configuration.generation_guard
    if factory is None:
        yield None
        return
    guard = factory(generation_nonce, deadline)
    enter, exit_ = _guard_protocol(guard)
    entered = enter(guard)
    launch = entered if isinstance(entered, GenerationLaunch) else None
    try:
        yield launch
    except BaseException as error:
        _exit_guard_after_failure(guard, exit_, error)
        raise
    else:
        exit_(guard, None, None, None)


def _guard_protocol(guard: object) -> tuple[Callable, Callable]:
    """The guard's enter and exit, refusing anything that is not a manager."""
    guard_type = type(guard)
    enter = getattr(guard_type, "__enter__", None)
    exit_ = getattr(guard_type, "__exit__", None)
    if not callable(enter) or not callable(exit_):
        raise TypeError("generation_guard must return a context manager")
    return enter, exit_


def _exit_guard_after_failure(
    guard: object, exit_: Callable, error: BaseException
) -> None:
    """Guard cleanup cannot suppress a failed generation and permit a commit."""
    try:
        exit_(guard, type(error), error, error.__traceback__)
    except BaseException as cleanup_error:
        _raise_cleanup_failures((cleanup_error,), prior_error=error)


def bootstrapped_state(
    generation_configuration: GenerationConfiguration,
    generation: Generation,
    startup_deadline: float,
) -> ProcessState:
    if generation_configuration.generation_bootstrap is None:
        return ProcessState.PROCESS_RUNNING
    return _run_generation_bootstrap(
        generation_configuration, generation, startup_deadline
    )


def _commit_heartbeat_failure_locked(
    instance: LspProcessHandle, coordinator: LifecycleCoordinator
) -> None:
    if not lsp_process_failure.select_terminal_failure_locked(
        instance, coordinator, "heartbeat_failed"
    ):
        return
    coordinator.phase = LifecyclePhase.STOPPING_FAILURE
    for generation in generations_locked(coordinator):
        mark_generation_expected_exit(generation)
    notify_lifecycle_locked(coordinator)


def _record_heartbeat_failure(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    error: BaseException,
    deadline: float,
) -> None:
    """A heartbeat that could not write its lease ends the lifecycle."""
    if not queue_owner_failure(coordinator, f"heartbeat_failed: {error}"):
        return
    if coordinator.cleanup_started.is_set():
        lsp_process_state.remember_background_cleanup_error(coordinator, error)
    try:
        acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        return
    try:
        _commit_heartbeat_failure_locked(instance, coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _heartbeat_once(instance: LspProcessHandle, coordinator: LifecycleCoordinator) -> bool:
    """One heartbeat; False when the loop should stop."""
    coordinator.heartbeat_wake.wait(lsp_process_limits.HEARTBEAT_SECONDS)
    coordinator.heartbeat_wake.clear()
    if coordinator.heartbeat_stop.is_set():
        return False
    maintenance_deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    try:
        write_current_lease(instance, maintenance_deadline)
    except BaseException as error:
        _record_heartbeat_failure(instance, coordinator, error, maintenance_deadline)
        return False
    return True


def heartbeat_loop(instance: LspProcessHandle) -> None:
    coordinator = instance._coordinator
    while _heartbeat_once(instance, coordinator):
        pass


_STOPPED_PHASES = frozenset(
    {LifecyclePhase.STOPPED_SUCCESS, LifecyclePhase.STOPPED_FAILURE}
)


def _lease_write_targets(
    coordinator: LifecycleCoordinator, deadline: float
) -> tuple[OwnerDirectory | None, Generation | None]:
    """The owner and generation the lease describes, or nothing when stopped."""
    acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.phase in _STOPPED_PHASES:
            return None, None
        return coordinator.owner_directory, coordinator.lease_generation
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _publish_current_lease(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    owner: OwnerDirectory,
    generation: Generation,
    deadline: float,
) -> None:
    write_generation_lease(
        owner,
        generation,
        instance.owner_nonce,
        deadline,
        coordinator.heartbeat_stop,
    )


def write_current_lease(instance: LspProcessHandle, deadline: float) -> None:
    coordinator = instance._coordinator
    if coordinator.heartbeat_stop.is_set():
        return
    lsp_process_state.acquire_lease(coordinator, deadline)
    try:
        owner, generation = _lease_write_targets(coordinator, deadline)
        if owner is None or generation is None:
            return
        _publish_current_lease(instance, coordinator, owner, generation, deadline)
    finally:
        release_lease(coordinator)


def _report_drain_inspection_timeout(coordinator: LifecycleCoordinator) -> None:
    """A maintenance pass that cannot take the lock records why it gave up."""
    bounded_error = TimeoutError(
        "LSP expired-drain inspection exceeded its maintenance deadline"
    )
    if queue_owner_failure(coordinator, str(bounded_error)):
        lsp_process_state.remember_background_cleanup_error(coordinator, bounded_error)


def _running_generation_locked(
    coordinator: LifecycleCoordinator,
) -> Generation | None:
    if coordinator.phase is not LifecyclePhase.RUNNING:
        return None
    return coordinator.active


def generation_exiting_locked(generation: Generation) -> bool:
    """Something has told us this generation is going; caller holds its lock."""
    if generation._exit_observed or generation.failure_queued:
        return True
    return generation.expected_exit.is_set()


def _generation_not_exiting(generation: Generation) -> bool:
    """Nothing has told us this generation is on its way out."""
    with generation.failure_lock:
        return not generation_exiting_locked(generation)


def active_drain_generation(
    instance: LspProcessHandle,
) -> tuple[Generation, LspProtocol] | None:
    coordinator = instance._coordinator
    deadline = time.monotonic() + lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    try:
        acquire_lifecycle(coordinator, deadline)
    except TimeoutError:
        _report_drain_inspection_timeout(coordinator)
        return None
    try:
        generation = _running_generation_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)
    return _drainable_generation(generation)


def _drainable_generation(
    generation: Generation | None,
) -> tuple[Generation, LspProtocol] | None:
    """The generation and its protocol, when it is live and not on its way out."""
    if generation is None or generation.protocol is None:
        return None
    if not _generation_not_exiting(generation):
        return None
    return generation, generation.protocol


def next_drain_deadline(instance: LspProcessHandle) -> float | None:
    active = active_drain_generation(instance)
    if active is None:
        return None
    return active[1].next_drain_deadline()


def queue_expired_drain(instance: LspProcessHandle) -> None:
    active = active_drain_generation(instance)
    if active is None:
        return
    generation, protocol = active
    if protocol.expired_drain_keys(time.monotonic()):
        lsp_process_state.queue_generation_failure(instance._coordinator, generation, "expired drain")
