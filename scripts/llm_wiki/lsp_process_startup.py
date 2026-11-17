"""Starting an LSP child and recording its initial evidence."""
from __future__ import annotations

import atexit
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from interruption import (
    interruption_in_chain as _interruption_in_chain,
)
from interruption import (
    raise_collected_errors as _raise_cleanup_failures,
)
from llm_wiki import (
    lsp_process_cleanup,
    lsp_process_generation,
    lsp_process_launch,
    lsp_process_limits,
    lsp_process_owner,
    lsp_process_recovery,
    lsp_process_state,
)
from llm_wiki.lsp_process_cleanup import (
    StartupCleanupError,
)
from llm_wiki.lsp_process_failure import (
    mark_terminal_failure,
    remember_mandatory_terminal_failure,
)
from llm_wiki.lsp_process_generation import (
    bootstrapped_state,
    generation_exiting_locked,
    generation_guard_context,
    publish_generation_lease,
)
from llm_wiki.lsp_process_launch import (
    generation_launch,
    validated_bootstrap_timeout,
    validated_command,
    validated_future_deadline,
    validated_generation_configuration,
)
from llm_wiki.lsp_process_limits import (
    STARTUP_FAILED,
)
from llm_wiki.lsp_process_owner import (
    OwnerDirectory,
)
from llm_wiki.lsp_process_state import (
    Generation,
    GenerationBootstrap,
    GenerationConfiguration,
    GenerationGuardFactory,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
    acquire_driver,
    acquire_lifecycle,
    coordinator_has_ownership,
    lsp_environment,
    notify_lifecycle_locked,
    release_driver,
    require_startup_deadline,
    unconfigured_generation,
    unregister_startup_cleanup,
)
from llm_wiki.lsp_process_sweep import (
    validated_owner_root,
)
from lsp_protocol import (
    LspProtocol,
)


def start_lsp_process(
    cls: type[LspProcessHandle],
    command: Sequence[str],
    *,
    cwd: Path,
    owner_root: Path,
) -> LspProcessHandle:
    return start_lsp_process_impl(
        cls,
        command,
        cwd=cwd,
        owner_root=owner_root,
        configured_deadline=None,
        generation_configuration=unconfigured_generation(),
    )


def start_configured_lsp_process(
    cls: type[LspProcessHandle],
    command: Sequence[str],
    *,
    cwd: Path,
    owner_root: Path,
    deadline: float,
    server_request_handlers: Mapping[str, Callable[[object], object]],
    server_notification_handlers: Mapping[str, Callable[[object], None]],
    generation_bootstrap: GenerationBootstrap,
    bootstrap_timeout_seconds: float | None,
    generation_guard: GenerationGuardFactory | None,
    environment_overrides: Mapping[str, str] | None = None,
) -> LspProcessHandle:
    configured_deadline = validated_future_deadline(deadline)
    if bootstrap_timeout_seconds is None:
        bootstrap_timeout_seconds = configured_deadline - time.monotonic()
    bootstrap_timeout_seconds = validated_bootstrap_timeout(
        bootstrap_timeout_seconds
    )
    configuration = validated_generation_configuration(
        server_request_handlers,
        server_notification_handlers,
        generation_bootstrap,
        bootstrap_timeout_seconds,
        generation_guard,
    )
    return start_lsp_process_impl(
        cls,
        command,
        cwd=cwd,
        owner_root=owner_root,
        configured_deadline=configured_deadline,
        generation_configuration=configuration,
        environment_overrides=environment_overrides,
    )


def _startup_already_terminal(
    coordinator: LifecycleCoordinator,
    generation: object,
    process: object,
    protocol: object,
) -> bool:
    """True when anything already recorded a terminal failure for this start."""
    if _terminal_recorded(coordinator):
        return True
    # The caller already holds this generation's failure lock, so the lock-free
    # form is the only one that can be used here.
    if generation_exiting_locked(generation):
        return True
    return process.poll() is not None or protocol.fatal


def _terminal_recorded(coordinator: LifecycleCoordinator) -> bool:
    """The coordinator already knows how this lifecycle ends."""
    if coordinator.terminal_outcome is not None:
        return True
    if coordinator.mandatory_failure_intent is not None:
        return True
    return coordinator.pending_failure_intents > 0 or coordinator.success_committed


def _promote_started_generation(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation: object,
    process: object,
    protocol: object,
    generation_state: ProcessState,
    startup_deadline: float,
) -> None:
    acquire_lifecycle(coordinator, startup_deadline)
    try:
        _activate_started_generation(
            instance, coordinator, generation, process, protocol, generation_state
        )
        notify_lifecycle_locked(coordinator)
    finally:
        lsp_process_state.release_lifecycle(coordinator)


def _activate_started_generation(
    instance: LspProcessHandle,
    coordinator: LifecycleCoordinator,
    generation: object,
    process: object,
    protocol: object,
    generation_state: ProcessState,
) -> None:
    with generation.failure_lock, coordinator.terminal_state_lock:
        if _startup_already_terminal(coordinator, generation, process, protocol):
            raise RuntimeError("LSP terminal failure was recorded during startup")
        coordinator.active = generation
        coordinator.candidate = None
        coordinator.lease_generation = generation
        coordinator.phase = LifecyclePhase.RUNNING
        coordinator.startup_complete = True
        instance.state = generation_state


def _startup_cleanup_errors(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    deadline: float,
) -> list[BaseException]:
    cleanup_errors: list[BaseException] = []
    try:
        mark_terminal_failure(instance, coordinator, STARTUP_FAILED, deadline)
    except BaseException as cleanup_error:
        cleanup_errors.append(cleanup_error)
    try:
        cleanup_errors.extend(
            lsp_process_cleanup.drive_cleanup(
                instance,
                deadline,
                terminal=True,
                failure_code=STARTUP_FAILED,
                coordinator_override=coordinator,
            )
        )
    except BaseException as cleanup_error:
        cleanup_errors.append(cleanup_error)
    return cleanup_errors


def _interrupted_startup(
    startup_error: BaseException, cleanup_errors: list[BaseException]
) -> bool:
    if _interruption_in_chain(startup_error) is not None:
        return True
    return any(_interruption_in_chain(error) is not None for error in cleanup_errors)


def _startup_cleanup_error(coordinator: LifecycleCoordinator) -> StartupCleanupError:
    message = "LSP startup cleanup retains retryable ownership"
    if coordinator.cleanup_result.evidence == "failed":
        message = (
            "LSP startup failed and retained evidence could not be written safely"
        )
    return StartupCleanupError(message, coordinator=coordinator)


def _fail_startup(
    instance: LspProcessHandle | None,
    coordinator: LifecycleCoordinator,
    startup_error: BaseException,
) -> None:
    """Record the failure, clean up, and raise what the caller must see."""
    deadline = lsp_process_launch.fresh_cleanup_deadline()
    remember_mandatory_terminal_failure(instance, coordinator, STARTUP_FAILED)
    cleanup_errors = _startup_cleanup_errors(instance, coordinator, deadline)
    pending = coordinator_has_ownership(coordinator)
    if pending:
        lsp_process_state.register_startup_cleanup(coordinator)
    _raise_startup_failure(coordinator, startup_error, cleanup_errors, pending)


def _raise_startup_failure(
    coordinator: LifecycleCoordinator,
    startup_error: BaseException,
    cleanup_errors: list[BaseException],
    pending: bool,
) -> None:
    if _interrupted_startup(startup_error, cleanup_errors):
        _raise_cleanup_failures(cleanup_errors, prior_error=startup_error)
    if pending:
        raise _startup_cleanup_error(coordinator) from startup_error
    raise startup_error


@dataclass
class _StartupSlot:
    """The instance a startup has built so far, for its failure path."""

    instance: LspProcessHandle | None = None


@dataclass
class _StartedGeneration:
    """What a successful launch produced inside the generation guard."""

    instance: LspProcessHandle
    generation: Generation
    process: object
    protocol: LspProtocol
    state: ProcessState


def _validated_cwd(cwd: Path) -> Path:
    cwd = Path(cwd)
    if not cwd.exists() or not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    return cwd.resolve()


def _claim_startup_ownership(
    coordinator: LifecycleCoordinator, owner_root: Path
) -> OwnerDirectory:
    """Open the owner directory this startup will own."""
    owner = OwnerDirectory.open(owner_root)
    coordinator.owner_directory = owner
    return owner


def _startup_owner_record(
    arguments: Sequence[str],
    *,
    generation_nonce: str,
    owner_nonce: str,
    started_at: str,
) -> dict[str, object]:
    return {
        "command_basename": Path(arguments[0]).name,
        "generation_nonce": generation_nonce,
        "owner_nonce": owner_nonce,
        "owner_pid": None,
        "started_at": started_at,
        "state": ProcessState.PROCESS_RUNNING.value,
    }


def _require_generation_owners(
    generation: Generation,
) -> tuple[object, LspProtocol]:
    process = generation.process
    protocol = generation.protocol
    if process is None or protocol is None:
        raise RuntimeError(
            "LSP generation did not acquire process and protocol owners"
        )
    return process, protocol


def _build_lsp_instance(
    cls: type[LspProcessHandle],
    coordinator: LifecycleCoordinator,
    generation: Generation,
    process: object,
    protocol: LspProtocol,
    *,
    owner_root: Path,
    owner_nonce: str,
    generation_nonce: str,
    started_monotonic: float,
    arguments: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> LspProcessHandle:
    instance = cls(
        process=process,
        protocol=protocol,
        owner_root=owner_root,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        state=ProcessState.PROCESS_RUNNING,
        started_monotonic=started_monotonic,
        last_used_monotonic=started_monotonic,
    )
    instance._coordinator = coordinator
    instance._command = tuple(arguments)
    instance._cwd = cwd
    instance._environment = dict(environment)
    instance._stderr_projection = generation.stderr
    instance._stderr_projection_lock = generation.stderr_lock
    return instance


def _launch_first_generation(
    cls: type[LspProcessHandle],
    coordinator: LifecycleCoordinator,
    arguments: Sequence[str],
    slot: _StartupSlot,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    owner_root: Path,
    owner_nonce: str,
    generation_nonce: str,
    owner_record: Mapping[str, object],
    started_monotonic: float,
    startup_deadline: float,
    generation_configuration: GenerationConfiguration,
) -> _StartedGeneration:
    """Start the first generation and the instance that owns it."""
    with generation_guard_context(
        generation_configuration, generation_nonce, startup_deadline
    ) as guarded_launch:
        generation_command, pass_fds = generation_launch(
            arguments,
            guarded_launch,
            cwd=cwd,
            owner_root=owner_root,
        )
        generation = lsp_process_generation.prepare_generation(
            coordinator,
            generation_command,
            cwd=cwd,
            environment=environment,
            deadline=startup_deadline,
            generation_nonce=generation_nonce,
            owner_record=owner_record,
            pass_fds=pass_fds,
        )
        require_startup_deadline(startup_deadline, "generation preparation")
        process, protocol = _require_generation_owners(generation)
        instance = _build_lsp_instance(
            cls,
            coordinator,
            generation,
            process,
            protocol,
            owner_root=owner_root,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            started_monotonic=started_monotonic,
            arguments=arguments,
            cwd=cwd,
            environment=environment,
        )
        slot.instance = instance
        publish_generation_lease(instance, generation, startup_deadline)
        require_startup_deadline(startup_deadline, "initial lease publication")
        lsp_process_recovery.start_heartbeat_worker(instance, startup_deadline)
        require_startup_deadline(startup_deadline, "heartbeat worker start")
        state = bootstrapped_state(
            generation_configuration, generation, startup_deadline
        )
    return _StartedGeneration(instance, generation, process, protocol, state)


def _finish_started_process(
    started: _StartedGeneration,
    coordinator: LifecycleCoordinator,
    owner: OwnerDirectory,
    startup_deadline: float,
) -> LspProcessHandle:
    """Publish the generation, start the workers, and prove the server is alive."""
    _promote_started_generation(
        started.instance,
        coordinator,
        started.generation,
        started.process,
        started.protocol,
        started.state,
        startup_deadline,
    )
    lsp_process_recovery.start_lifecycle_workers(started.instance, startup_deadline)
    require_startup_deadline(startup_deadline, "lifecycle worker start")
    owner.verify_lexical_identity()
    if started.process.poll() is not None or started.protocol.fatal:
        raise RuntimeError("LSP process exited during startup")
    require_startup_deadline(startup_deadline, "final startup fence")
    atexit.register(started.instance._atexit_close)
    unregister_startup_cleanup(coordinator)
    return started.instance


def resolved_startup_deadline(configured_deadline: float | None) -> float:
    """The caller's deadline, or the default startup budget from now."""
    if configured_deadline is None:
        return time.monotonic() + lsp_process_limits.STARTUP_WAIT_SECONDS
    return configured_deadline


def _release_startup_driver(
    coordinator: LifecycleCoordinator, driver_acquired: bool
) -> None:
    """Release the driver, then drop the registration if nothing is held."""
    try:
        if driver_acquired:
            release_driver(coordinator)
    finally:
        if not coordinator_has_ownership(coordinator):
            unregister_startup_cleanup(coordinator)


def start_lsp_process_impl(
    cls: type[LspProcessHandle],
    command: Sequence[str],
    *,
    cwd: Path,
    owner_root: Path,
    configured_deadline: float | None,
    generation_configuration: GenerationConfiguration,
    environment_overrides: Mapping[str, str] | None = None,
) -> LspProcessHandle:
    cwd = _validated_cwd(cwd)
    owner_root = Path(owner_root)
    arguments = validated_command(command, cwd)
    environment = lsp_environment(extra=environment_overrides)
    owner_nonce = validated_owner_root(owner_root)
    generation_nonce = lsp_process_owner.new_generation_nonce()
    started_monotonic = time.monotonic()
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    coordinator = LifecycleCoordinator(
        None,
        generation_configuration=generation_configuration,
        startup_generation_nonce=generation_nonce,
    )
    lsp_process_state.register_startup_cleanup(coordinator)
    slot = _StartupSlot()
    driver_acquired = False

    try:
        owner = _claim_startup_ownership(coordinator, owner_root)
        startup_deadline = resolved_startup_deadline(configured_deadline)
        acquire_driver(coordinator, startup_deadline)
        driver_acquired = True
        owner.create(startup_deadline)
        require_startup_deadline(startup_deadline, "owner creation")
        started = _launch_first_generation(
            cls,
            coordinator,
            arguments,
            slot,
            cwd=cwd,
            environment=environment,
            owner_root=owner_root,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            owner_record=_startup_owner_record(
                arguments,
                generation_nonce=generation_nonce,
                owner_nonce=owner_nonce,
                started_at=started_at,
            ),
            started_monotonic=started_monotonic,
            startup_deadline=startup_deadline,
            generation_configuration=generation_configuration,
        )
        require_startup_deadline(startup_deadline, "generation guard completion")
        return _finish_started_process(started, coordinator, owner, startup_deadline)
    except BaseException as startup_error:
        _fail_startup(slot.instance, coordinator, startup_error)
    finally:
        _release_startup_driver(coordinator, driver_acquired)
