"""The shared state of an LSP child, its phases, generations and lifecycle coordinator."""
from __future__ import annotations

import contextlib
import errno
import json
import os
import queue
import re
import selectors
import subprocess as _subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

import lsp_process_tree as _lsp_process_tree
from interruption import walk_exception_chain
from llm_wiki import lsp_process_limits
from llm_wiki.lsp_process_limits import (
    CLEANUP_STEPS,
    LSP_ENV_ALLOWLIST,
    MAX_STARTUP_CLEANUP_OWNERS,
    MAX_STDERR_BYTES,
    PROCESS_EXITED,
    STDERR_CHUNK_BYTES,
    STDERR_TAIL_BYTES,
)
from llm_wiki.lsp_process_owner import (
    OwnerDirectory,
)
from lsp_protocol import (
    LspProtocol,
    ProtocolStartupCleanupError,
)

ProcessTree = _lsp_process_tree.ProcessTree


class _SubprocessFacade:
    Popen = _subprocess.Popen
    PIPE = _subprocess.PIPE
    TimeoutExpired = _subprocess.TimeoutExpired


subprocess = _SubprocessFacade()
_lsp_process_tree.subprocess = subprocess


class ProcessState(str, Enum):  # noqa: UP042 - preserve existing Enum string and format behavior
    PROCESS_RUNNING = "process_running"
    PROTOCOL_INITIALIZED = "protocol_initialized"
    WORKSPACE_READY = "workspace_ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GenerationLaunch:
    command: tuple[str, ...]
    pass_fds: tuple[int, ...] = ()


GenerationBootstrap = Callable[[LspProtocol, int, str, float], ProcessState]
GenerationGuardFactory = Callable[[str, float], AbstractContextManager[object]]


@dataclass(frozen=True, slots=True)
class GenerationConfiguration:
    server_request_handlers: Mapping[str, Callable[[object], object]]
    server_notification_handlers: Mapping[str, Callable[[object], None]]
    generation_bootstrap: GenerationBootstrap | None
    bootstrap_timeout_seconds: float | None
    generation_guard: GenerationGuardFactory | None


def unconfigured_generation() -> GenerationConfiguration:
    return GenerationConfiguration(
        MappingProxyType({}), MappingProxyType({}), None, None, None
    )


class LifecyclePhase(str, Enum):  # noqa: UP042 - preserve existing Enum string and format behavior
    STARTING = "starting"
    RUNNING = "running"
    RECOVERY_PENDING = "recovery_pending"
    RESTARTING = "restarting"
    STOPPING_SUCCESS = "stopping_success"
    STOPPING_FAILURE = "stopping_failure"
    CLEANUP_PENDING = "cleanup_pending"
    STOPPED_SUCCESS = "stopped_success"
    STOPPED_FAILURE = "stopped_failure"


@dataclass(frozen=True, slots=True)
class FailureIntent:
    generation_nonce: str | None
    reason: str
    owner_fatal: bool
    observed_monotonic: float


@dataclass(frozen=True, slots=True)
class FailureEvidenceIdentity:
    code: str
    owner_nonce: str
    generation_nonce: str
    pid: int | None
    stderr_tail: str | None = None


@dataclass(slots=True)
class StderrWake:
    """The pipe a POSIX drain thread watches beside stderr; closing it stops the thread.

    A server that left its process group keeps stderr's write end open, so
    end-of-file is not ours to wait for. The thread owns `read_fd`; cleanup
    owns `write_fd`.
    """

    read_fd: int | None
    write_fd: int | None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    close_errors: dict[str, BaseException] = field(default_factory=dict, repr=False)

    def _close_end(self, name: str) -> None:
        """Retire ownership before closing; an uncertain close is never retried."""
        with self.lock:
            error = self.close_errors.get(name)
            if error is not None:
                raise error
            descriptor = getattr(self, name)
            if descriptor is None:
                return
            setattr(self, name, None)
            try:
                os.close(descriptor)
            except BaseException as error:
                self.close_errors[name] = error
                raise

    def request(self) -> None:
        """Ask the drain thread to finish; asking twice is harmless."""
        self._close_end("write_fd")

    def close_read(self) -> None:
        """The drain owner, or cleanup of an unstarted drain, closes this end once."""
        self._close_end("read_fd")

    def require_known_closes(self) -> None:
        with self.lock:
            if self.close_errors:
                raise next(iter(self.close_errors.values()))

    def owned_read_fd(self) -> int:
        with self.lock:
            descriptor = self.read_fd
        if descriptor is None:
            raise OSError(errno.EBADF, "LSP stderr wake read ownership was released")
        return descriptor

    def abandon(self) -> None:
        """Both ends, for a drain thread that was never started."""
        self.request()
        self.close_read()


@dataclass(slots=True)
class Generation:
    nonce: str
    tree: ProcessTree | None
    process: subprocess.Popen[bytes] | None
    server_pid: int | None = None
    windows_job: int | None = None
    protocol: LspProtocol | None = None
    stderr: deque[bytes] = field(default_factory=deque)
    stderr_size: list[int] = field(default_factory=lambda: [0])
    stderr_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    stderr_thread: threading.Thread | None = None
    stderr_wake: StderrWake | None = None
    exit_thread: threading.Thread | None = None
    expected_exit: threading.Event = field(default_factory=threading.Event, repr=False)
    failure_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    failure_queued: bool = field(default=False, repr=False)
    _exit_observed: bool = field(default=False, init=False, repr=False)

    @property
    def released(self) -> bool:
        """Nothing of this generation is still held."""
        held = (
            self.tree,
            self.process,
            self.windows_job,
            self.protocol,
            self.stderr_thread,
            self.exit_thread,
        )
        return all(item is None for item in held)


@dataclass(frozen=True, slots=True)
class CleanupError:
    step: str
    error_type: str
    error_code: int | None


_REPORTABLE_ERROR_CATEGORIES = (
    TimeoutError,
    PermissionError,
    FileNotFoundError,
    OSError,
    ValueError,
    RuntimeError,
)


def _reportable_error_type(error: BaseException) -> str:
    """The narrowest category we are willing to name in evidence."""
    for category in _REPORTABLE_ERROR_CATEGORIES:
        if isinstance(error, category):
            return category.__name__
    return "Exception"


def _reportable_error_code(error: BaseException) -> int | None:
    """The platform error code, when it is a plain unsigned 32-bit integer."""
    for attribute in ("winerror", "errno"):
        candidate = _error_attribute(error, attribute)
        if _is_reportable_code(candidate):
            return candidate
    return None


def _error_attribute(error: BaseException, attribute: str) -> object:
    """One attribute of an exception, where reading it may itself fail."""
    try:
        return getattr(error, attribute, None)
    except BaseException:
        return None


def _is_reportable_code(candidate: object) -> bool:
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        return False
    return 0 <= candidate <= 0xFFFFFFFF


def _sanitized_cleanup_error(step: str, error: BaseException) -> CleanupError:
    return CleanupError(
        step, _reportable_error_type(error), _reportable_error_code(error)
    )


def protocol_startup_cleanup_in_chain(
    error: BaseException,
) -> ProtocolStartupCleanupError | None:
    """The retained protocol cleanup owner anywhere in the error's chain."""
    for current in walk_exception_chain(error):
        if isinstance(current, ProtocolStartupCleanupError):
            return current
    return None


@dataclass(slots=True)
class CleanupResult:
    tree_termination: str = "pending"
    tree_release: str = "pending"
    protocol_stop: str = "pending"
    generation_joins: str = "pending"
    heartbeat_join: str = "pending"
    recovery_join: str = "pending"
    lease_removal: str = "pending"
    evidence: str = "pending"
    scratch_removal: str = "pending"
    owner_handles: str = "pending"
    errors: list[CleanupError] = field(default_factory=list)
    ownership_pending: bool = True

    def failed(self, step: str, error: BaseException) -> None:
        if step not in CLEANUP_STEPS:
            raise ValueError("unknown LSP cleanup step")
        setattr(self, step, "failed")
        self._drop_step_errors(step)
        self.errors.append(_sanitized_cleanup_error(step, error))

    def succeeded(self, step: str, status: str = "success") -> None:
        if step not in CLEANUP_STEPS or status not in {"success", "not_applicable"}:
            raise ValueError("invalid LSP cleanup resolution")
        setattr(self, step, status)
        self._drop_step_errors(step)

    def _drop_step_errors(self, step: str) -> None:
        self.errors[:] = [item for item in self.errors if item.step != step]


@dataclass(slots=True)
class LifecycleCoordinator:
    owner_directory: OwnerDirectory | None
    startup_generation_nonce: str | None = None
    generation_configuration: GenerationConfiguration = field(
        default_factory=unconfigured_generation
    )
    phase: LifecyclePhase = LifecyclePhase.STARTING
    active: Generation | None = None
    candidate: Generation | None = None
    lease_generation: Generation | None = None
    retired: list[Generation] = field(default_factory=list)
    terminal_outcome: str | None = None
    terminal_code: str | None = None
    failure_evidence_identity: FailureEvidenceIdentity | None = None
    mandatory_failure_intent: FailureEvidenceIdentity | None = None
    background_cleanup_error: CleanupError | None = field(default=None, repr=False)
    background_restart_error: CleanupError | None = field(default=None, repr=False)
    background_error_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    cleanup_result: CleanupResult = field(default_factory=CleanupResult)
    recovery_attempted: bool = False
    recovery_request_nonce: str | None = None
    startup_complete: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    condition: threading.Condition = field(init=False, repr=False)
    driver: threading.RLock = field(default_factory=threading.RLock, repr=False)
    lease_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    terminal_intent_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    terminal_state_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    pending_failure_intents: int = 0
    pending_failure_code: str | None = None
    success_committed: bool = False
    cleanup_started: threading.Event = field(default_factory=threading.Event, repr=False)
    failure_queue: queue.SimpleQueue[FailureIntent] = field(
        default_factory=queue.SimpleQueue, repr=False
    )
    recovery_request_pending: threading.Event = field(
        default_factory=threading.Event, repr=False
    )
    recovery_wake: threading.Event = field(default_factory=threading.Event, repr=False)
    recovery_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    recovery_thread: threading.Thread | None = None
    heartbeat_stop: threading.Event = field(default_factory=threading.Event, repr=False)
    heartbeat_wake: threading.Event = field(default_factory=threading.Event, repr=False)
    heartbeat_thread: threading.Thread | None = None
    seen_failures: set[tuple[str | None, bool]] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


_STARTUP_CLEANUP_LOCK = threading.Lock()
_STARTUP_CLEANUPS: dict[int, LifecycleCoordinator] = {}


def register_startup_cleanup(coordinator: LifecycleCoordinator) -> None:
    with _STARTUP_CLEANUP_LOCK:
        key = id(coordinator)
        if key in _STARTUP_CLEANUPS:
            return
        if len(_STARTUP_CLEANUPS) >= MAX_STARTUP_CLEANUP_OWNERS:
            raise RuntimeError("LSP startup cleanup registry bound was exhausted")
        _STARTUP_CLEANUPS[key] = coordinator


def unregister_startup_cleanup(coordinator: LifecycleCoordinator) -> None:
    with _STARTUP_CLEANUP_LOCK:
        _STARTUP_CLEANUPS.pop(id(coordinator), None)


def pending_startup_cleanup_snapshot() -> tuple[LifecycleCoordinator, ...]:
    with _STARTUP_CLEANUP_LOCK:
        return tuple(_STARTUP_CLEANUPS.values())


def _check_environment_pair(name: object, value: object) -> None:
    """One inherited name and value: a string without a NUL."""
    if not isinstance(name, str) or not isinstance(value, str):
        raise TypeError("environment names and values must be strings")
    if "\0" in name or "\0" in value:
        raise ValueError("environment names and values must not contain NUL")


def _check_environment_pairs(values: Mapping[str, str]) -> None:
    for name, value in values.items():
        _check_environment_pair(name, value)


def _is_existing_directory(path: Path) -> bool:
    return path.is_absolute() and path.is_dir()


def _check_windows_system_root(values: Mapping[str, str]) -> None:
    """Windows needs an inherited SYSTEMROOT that actually exists."""
    if os.name != "nt":
        return
    system_root = values.get("SYSTEMROOT")
    if not system_root or not _is_existing_directory(Path(system_root)):
        raise ValueError(
            "SYSTEMROOT must be an inherited existing directory on Windows"
        )


def lsp_environment(
    source: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a new, canonical environment containing only required process values.

    `extra` is a profile's own declaration — gopls needs its pinned toolchain on
    `PATH` and its caches inside the managed root, and a server that is a
    compiler's client cannot be launched with the allowlist alone. It is code,
    not operator input, and every pair is checked like an inherited one.
    Research: `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
    """
    values = os.environ if source is None else source
    _check_environment_pairs(values)
    _check_windows_system_root(values)
    environment = {
        name: values[name] for name in sorted(LSP_ENV_ALLOWLIST) if name in values
    }
    return _with_profile_environment(environment, extra)


def _with_profile_environment(
    environment: dict[str, str], extra: Mapping[str, str] | None
) -> dict[str, str]:
    if not extra:
        return environment
    _check_environment_pairs(extra)
    environment.update(dict(extra))
    return environment


def acquire_lifecycle(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.lock.acquire(blocking=False)
    else:
        acquired = coordinator.lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP lifecycle transition lock deadline expired")


def release_lifecycle(coordinator: LifecycleCoordinator) -> None:
    coordinator.lock.release()


def acquire_driver(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.driver.acquire(blocking=False)
    else:
        acquired = coordinator.driver.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP lifecycle driver deadline expired")


def release_driver(coordinator: LifecycleCoordinator) -> None:
    coordinator.driver.release()


def acquire_lease(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.lease_lock.acquire(blocking=False)
    else:
        acquired = coordinator.lease_lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP lease lock deadline expired")


def release_lease(coordinator: LifecycleCoordinator) -> None:
    coordinator.lease_lock.release()


def acquire_terminal_intent(
    coordinator: LifecycleCoordinator,
    deadline: float,
    *,
    allow_expired: bool = False,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        acquired = allow_expired and coordinator.terminal_intent_lock.acquire(
            blocking=False
        )
    else:
        acquired = coordinator.terminal_intent_lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError("LSP terminal intent lock deadline expired")


def release_terminal_intent(coordinator: LifecycleCoordinator) -> None:
    coordinator.terminal_intent_lock.release()


def notify_lifecycle_locked(coordinator: LifecycleCoordinator) -> None:
    coordinator.condition.notify_all()


def require_startup_deadline(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError(f"LSP startup deadline expired after {stage}")


def assign_candidate(
    coordinator: LifecycleCoordinator,
    generation: Generation,
    deadline: float,
) -> None:
    acquire_lifecycle(coordinator, deadline)
    try:
        if coordinator.candidate is not None and coordinator.candidate is not generation:
            raise RuntimeError("another LSP candidate already owns lifecycle resources")
        coordinator.candidate = generation
        notify_lifecycle_locked(coordinator)
    finally:
        release_lifecycle(coordinator)


def queue_generation_failure(
    coordinator: LifecycleCoordinator,
    generation: Generation,
    reason: str,
) -> bool:
    return commit_generation_failure(
        coordinator,
        generation,
        reason,
        exit_observed=False,
    )


def commit_generation_failure(
    coordinator: LifecycleCoordinator,
    generation: Generation,
    reason: str,
    *,
    exit_observed: bool,
) -> bool:
    with generation.failure_lock:
        if exit_observed:
            generation._exit_observed = True
        return _queue_first_failure_locked(coordinator, generation, reason)


def _queue_first_failure_locked(
    coordinator: LifecycleCoordinator, generation: Generation, reason: str
) -> bool:
    """Queue the generation's first unexpected failure; the caller holds its failure lock."""
    if generation.expected_exit.is_set() or generation.failure_queued:
        return False
    intent = FailureIntent(generation.nonce, reason, False, time.monotonic())
    if not enqueue_failure_intent(coordinator, intent):
        return False
    generation.failure_queued = True
    return True


def mark_generation_expected_exit(generation: Generation) -> bool:
    with generation.failure_lock:
        if generation._exit_observed or generation.failure_queued:
            return False
        generation.expected_exit.set()
        return True


def enqueue_failure_intent(
    coordinator: LifecycleCoordinator, intent: FailureIntent
) -> bool:
    with coordinator.terminal_state_lock:
        if coordinator.success_committed:
            return False
        coordinator.pending_failure_intents += 1
        if coordinator.pending_failure_code is None:
            coordinator.pending_failure_code = (
                "heartbeat_failed" if intent.owner_fatal else PROCESS_EXITED
            )
        coordinator.failure_queue.put(intent)
    coordinator.recovery_wake.set()
    return True


def acknowledge_failure_intent(coordinator: LifecycleCoordinator) -> None:
    with coordinator.terminal_state_lock:
        if coordinator.pending_failure_intents <= 0:
            raise RuntimeError("LSP failure intent acknowledgement underflow")
        coordinator.pending_failure_intents -= 1
        if coordinator.pending_failure_intents == 0:
            coordinator.pending_failure_code = None


def queue_owner_failure(
    coordinator: LifecycleCoordinator, reason: str
) -> bool:
    return enqueue_failure_intent(
        coordinator,
        FailureIntent(None, reason, True, time.monotonic()),
    )


def remember_background_cleanup_error(
    coordinator: LifecycleCoordinator, error: BaseException
) -> None:
    with coordinator.background_error_lock:
        if coordinator.background_cleanup_error is None:
            coordinator.background_cleanup_error = _sanitized_cleanup_error(
                "background_cleanup", error
            )


def take_background_cleanup_error(
    coordinator: LifecycleCoordinator,
) -> BaseException | None:
    with coordinator.background_error_lock:
        error = coordinator.background_cleanup_error
        coordinator.background_cleanup_error = None
    if error is None:
        return None
    code = f", code {error.error_code}" if error.error_code is not None else ""
    return RuntimeError(f"LSP background cleanup failed ({error.error_type}{code})")


def remember_background_restart_error(
    coordinator: LifecycleCoordinator,
    error: BaseException,
) -> None:
    with coordinator.background_error_lock:
        if coordinator.background_restart_error is None:
            coordinator.background_restart_error = _sanitized_cleanup_error(
                "restart",
                error,
            )


def take_background_restart_error(
    coordinator: LifecycleCoordinator,
) -> BaseException | None:
    with coordinator.background_error_lock:
        error = coordinator.background_restart_error
        coordinator.background_restart_error = None
    if error is None:
        return None
    code = f", code {error.error_code}" if error.error_code is not None else ""
    return RuntimeError(f"LSP replacement startup cause ({error.error_type}{code})")


def generations_locked(coordinator: LifecycleCoordinator) -> list[Generation]:
    generations: list[Generation] = []
    identities: set[int] = set()
    for generation in [
        coordinator.active,
        coordinator.candidate,
        *coordinator.retired,
    ]:
        if generation is not None and id(generation) not in identities:
            identities.add(id(generation))
            generations.append(generation)
    return generations


def _any_worker_alive(coordinator: LifecycleCoordinator) -> bool:
    """Either lifecycle worker is still running."""
    workers = (coordinator.heartbeat_thread, coordinator.recovery_thread)
    return any(thread is not None and thread.is_alive() for thread in workers)


def _any_generation_held(coordinator: LifecycleCoordinator) -> bool:
    """Some generation still holds something of ours."""
    return any(not item.released for item in generations_locked(coordinator))


def _owner_directory_open(coordinator: LifecycleCoordinator) -> bool:
    owner = coordinator.owner_directory
    return owner is not None and not owner._closed


def coordinator_has_ownership_locked(coordinator: LifecycleCoordinator) -> bool:
    if _any_generation_held(coordinator) or _any_worker_alive(coordinator):
        return True
    return _owner_directory_open(coordinator)


def coordinator_has_ownership(coordinator: LifecycleCoordinator) -> bool:
    if not coordinator.lock.acquire(timeout=lsp_process_limits.GRACEFUL_CLEANUP_SECONDS):
        return True
    try:
        return coordinator_has_ownership_locked(coordinator)
    finally:
        coordinator.lock.release()


def write_owner_record(
    owner_directory: OwnerDirectory,
    record: Mapping[str, object],
) -> None:
    owner_directory.write_record("owner.json", record)


def new_stderr_wake() -> StderrWake | None:
    """A wake pipe where a pipe can be selected on; Windows ends stderr itself.

    There the Job Object kills every holder of the write end, so end-of-file
    always arrives and the drain thread needs no second descriptor.
    """
    if os.name == "nt":
        return None
    read_fd, write_fd = os.pipe()
    return StderrWake(read_fd, write_fd)


@dataclass(frozen=True, slots=True)
class _StderrRing:
    """Where the drain thread keeps the last bytes the server wrote."""

    chunks: deque[bytes]
    size: list[int]
    lock: threading.Lock


def drain_stderr(
    stream: BinaryIO,
    chunks: deque[bytes],
    size: list[int],
    lock: threading.Lock,
    wake: StderrWake | None = None,
) -> None:
    """Own the server's stderr: read it as it arrives and close it at the end.

    Nothing else reads or closes this stream while the thread lives, so no
    cleanup can wait behind a read that an escaped process keeps open.
    """
    try:
        _read_stderr_forever(stream, _StderrRing(chunks, size, lock), wake)
    except (OSError, ValueError):
        return
    finally:
        _close_stderr_ends(stream, wake)


def _close_stderr_ends(stream: BinaryIO, wake: StderrWake | None) -> None:
    with contextlib.suppress(OSError, ValueError):
        stream.close()
    if wake is not None:
        with contextlib.suppress(OSError):
            wake.close_read()


def _take_stderr_chunk(descriptor: int, ring: _StderrRing) -> bool:
    """Append what the pipe holds now; False once the stream has ended."""
    chunk = os.read(descriptor, STDERR_CHUNK_BYTES)
    if not chunk:
        return False
    with ring.lock:
        ring.chunks.append(chunk)
        ring.size[0] += len(chunk)
        _trim_stderr_chunks(ring.chunks, ring.size)
    return True


def _ready_descriptors(selector: selectors.BaseSelector, timeout: float | None) -> set[int]:
    return {key.fd for key, _events in selector.select(timeout)}


def _take_waiting_stderr(
    selector: selectors.BaseSelector, descriptor: int, ring: _StderrRing
) -> None:
    """After the wake-up: what is already in the pipe, and never more than the ring."""
    for _ in range(MAX_STDERR_BYTES // STDERR_CHUNK_BYTES + 1):
        if descriptor not in _ready_descriptors(selector, 0):
            return
        if not _take_stderr_chunk(descriptor, ring):
            return


def _read_stderr_until_woken(
    selector: selectors.BaseSelector, descriptor: int, wake_fd: int, ring: _StderrRing
) -> None:
    while wake_fd not in _ready_descriptors(selector, None):
        if not _take_stderr_chunk(descriptor, ring):
            return
    _take_waiting_stderr(selector, descriptor, ring)


def _read_stderr_forever(
    stream: BinaryIO, ring: _StderrRing, wake: StderrWake | None
) -> None:
    """Append every chunk the server writes until its stream ends or we are woken."""
    descriptor = stream.fileno()
    if wake is None:
        while _take_stderr_chunk(descriptor, ring):
            pass
        return
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        wake_fd = wake.owned_read_fd()
        selector.register(wake_fd, selectors.EVENT_READ)
        _read_stderr_until_woken(selector, descriptor, wake_fd, ring)


def _trim_stderr_chunks(chunks: deque[bytes], size: list[int]) -> None:
    """Drop the oldest bytes until the projection fits its bound."""
    excess = size[0] - MAX_STDERR_BYTES
    while excess > 0:
        oldest = chunks[0]
        if len(oldest) > excess:
            chunks[0] = oldest[excess:]
            size[0] -= excess
            return
        chunks.popleft()
        size[0] -= len(oldest)
        excess -= len(oldest)


def write_failure_record(
    owner_directory: OwnerDirectory,
    *,
    code: str,
    owner_nonce: str,
    generation_nonce: str,
    pid: int | None,
    stderr_tail: str | None = None,
) -> None:
    failure_record: dict[str, object] = {
        "code": code,
        "generation_nonce": generation_nonce,
        "owner_nonce": owner_nonce,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if pid is not None:
        failure_record["server_pid"] = pid
    if stderr_tail is not None:
        failure_record["stderr_tail"] = stderr_tail
    validate_failure_record(
        failure_record,
        code=code,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        pid=pid,
        stderr_tail=stderr_tail,
    )
    # A verified Windows owner DACL has one inheritable owner-only (OI)(CI) ACE.
    owner_directory.write_record("failure.json", failure_record)


_FAILURE_REQUIRED_FIELDS = frozenset(
    {"code", "generation_nonce", "owner_nonce", "timestamp"}
)
_FAILURE_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
_FAILURE_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")
_FAILURE_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z"
)


def _fully_matches(value: object, pattern: re.Pattern[str]) -> bool:
    """A string the pattern accepts end to end; anything else is not one."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def is_server_pid(value: object) -> bool:
    """A true integer above zero; a bool is not a process identifier."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


_FAILURE_OPTIONAL_FIELDS = frozenset({"server_pid", "stderr_tail"})


def _validate_failure_shape(record: Mapping[str, object]) -> None:
    names = frozenset(record)
    known = _FAILURE_REQUIRED_FIELDS | _FAILURE_OPTIONAL_FIELDS
    if not _FAILURE_REQUIRED_FIELDS <= names or not names <= known:
        raise ValueError("LSP failure evidence has an invalid shape")


def _validate_failure_code(record: Mapping[str, object]) -> None:
    if not _fully_matches(record["code"], _FAILURE_CODE_PATTERN):
        raise ValueError("LSP failure evidence has an invalid code")


def _validate_failure_nonces(record: Mapping[str, object]) -> None:
    for name in ("generation_nonce", "owner_nonce"):
        if not _fully_matches(record[name], _FAILURE_NONCE_PATTERN):
            raise ValueError(f"LSP failure evidence has an invalid {name}")


def _rendered_utc_timestamp(timestamp: str) -> str | None:
    """The stamp as its writer would render it, or None when it is unreadable."""
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo != UTC:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _validate_failure_timestamp(record: Mapping[str, object]) -> None:
    timestamp = record["timestamp"]
    if not _fully_matches(timestamp, _FAILURE_TIMESTAMP_PATTERN):
        raise ValueError("LSP failure evidence has an invalid timestamp")
    if _rendered_utc_timestamp(str(timestamp)) != timestamp:
        raise ValueError("LSP failure evidence has an invalid timestamp")


def _validate_failure_pid(record: Mapping[str, object]) -> None:
    if "server_pid" not in record:
        return
    if not is_server_pid(record["server_pid"]):
        raise ValueError("LSP failure evidence has an invalid server_pid")


def _expected_failure_identity(
    *,
    code: str,
    owner_nonce: str,
    generation_nonce: str,
    pid: int | None,
    stderr_tail: str | None,
) -> dict[str, object]:
    """The exact field values this terminal record has to carry."""
    identity: dict[str, object] = {
        "code": code,
        "owner_nonce": owner_nonce,
        "generation_nonce": generation_nonce,
    }
    optional = (("server_pid", pid), ("stderr_tail", stderr_tail))
    identity.update({name: value for name, value in optional if value is not None})
    return identity


def _validate_failure_identity(
    record: Mapping[str, object], expected: Mapping[str, object]
) -> None:
    observed = {name: record.get(name) for name in expected}
    expected_fields = _FAILURE_REQUIRED_FIELDS | (
        frozenset(expected) & _FAILURE_OPTIONAL_FIELDS
    )
    if observed != dict(expected) or frozenset(record) != expected_fields:
        raise ValueError("LSP failure evidence does not match expected terminal identity")


def _validate_failure_tail(record: Mapping[str, object]) -> None:
    if "stderr_tail" not in record:
        return
    tail = record["stderr_tail"]
    if not isinstance(tail, str) or not 0 < len(json.dumps(tail)) <= STDERR_TAIL_BYTES:
        raise ValueError("LSP failure evidence has an invalid stderr_tail")


def validate_failure_record(
    record: Mapping[str, object],
    *,
    code: str,
    owner_nonce: str,
    generation_nonce: str,
    pid: int | None,
    stderr_tail: str | None = None,
) -> None:
    _validate_failure_shape(record)
    _validate_failure_code(record)
    _validate_failure_nonces(record)
    _validate_failure_timestamp(record)
    _validate_failure_pid(record)
    _validate_failure_tail(record)
    _validate_failure_identity(
        record,
        _expected_failure_identity(
            code=code,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            pid=pid,
            stderr_tail=stderr_tail,
        ),
    )


class LspProcessHandle:
    """The projections every LSP process offers its lifecycle parts; the fields live on `LspProcess`."""

    __slots__ = ()

    def stderr_bytes(self) -> bytes:
        with self._stderr_projection_lock:
            return b"".join(self._stderr_projection)

    def _projected_generation(self) -> Generation | None:
        coordinator = self._coordinator
        if coordinator.active is not None:
            return coordinator.active
        if coordinator.candidate is not None:
            return coordinator.candidate
        return self._retired_generation(coordinator)

    def _retired_generation(self, coordinator: LifecycleCoordinator) -> Generation | None:
        return next(
            (generation for generation in coordinator.retired if generation.nonce == self.generation_nonce),
            None,
        )

    @property
    def _tree(self) -> ProcessTree | None:
        generation = self._projected_generation()
        return generation.tree if generation is not None else None

    @property
    def _owner_directory(self) -> OwnerDirectory | None:
        return self._coordinator.owner_directory

    @property
    def _stderr_thread(self) -> threading.Thread | None:
        generation = self._projected_generation()
        return generation.stderr_thread if generation is not None else None

    @property
    def _exit_thread(self) -> threading.Thread | None:
        generation = self._projected_generation()
        return generation.exit_thread if generation is not None else None

    @property
    def _heartbeat_thread(self) -> threading.Thread | None:
        return self._coordinator.heartbeat_thread

    @property
    def _recovery_thread(self) -> threading.Thread | None:
        return self._coordinator.recovery_thread
