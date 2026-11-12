"""Admit, launch, pace and retire language-server processes for one session."""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from interruption import (
    interruption_in_chain as startup_interruption,
)
from interruption import (
    raise_collected_errors as _raise_collected_errors,
)
from llm_wiki.pyright_session_launch import LaunchServerGuard
from llm_wiki.pyright_session_paths import ensure_lsp_parent
from llm_wiki.pyright_session_values import (
    OWNER_CLEANUP_SECONDS,
    BootstrapDegradation,
    validated_deadline,
)
from lsp_paths import lsp_owner_root
from lsp_process import LspProcess, ProcessState, StartupCleanupError
from lsp_protocol import JsonRpcResponseError, ProtocolViolation

# A Pyright session's startup budget, capped by the caller's deadline (docs/superpowers/plans/2026-07-22-python-pyright-navigation.md).
STARTUP_SECONDS = 60.0


def _permission_startup_code(error: PermissionError, prefix: str) -> str | None:
    """A refused permission that is really a timeout in disguise."""
    cause = error.__cause__
    if cause is not None and cause.__class__.__name__ == "TimeoutExpired":
        return f"{prefix}_startup_timeout"
    if "deadline expired" in str(error):
        return f"{prefix}_startup_timeout"
    return None


def _timeout_startup_code(error: BaseException, prefix: str) -> str | None:
    """A timeout, or a permission refusal that is really one."""
    if isinstance(error, TimeoutError):
        return f"{prefix}_startup_timeout"
    if isinstance(error, PermissionError):
        return _permission_startup_code(error, prefix)
    return None


def _startup_code_of(error: BaseException, prefix: str) -> str | None:
    """The code this one error names, if it names one."""
    if isinstance(error, BootstrapDegradation):
        return error.code
    return _timeout_startup_code(error, prefix)


def _startup_code(error: BaseException, prefix: str = "pyright") -> str:
    current: BaseException | None = error
    while current is not None:
        code = _startup_code_of(current, prefix)
        if code is not None:
            return code
        current = current.__cause__
    return f"{prefix}_startup_failed"


# How long a session waits before it tries a failed start again, and -- by its
# length -- how many times it tries at all. Microsoft's retry guidance for an
# interactive caller is "a smaller number of retries with only a short delay";
# a language server is heavier than an HTTP call, so the delays grow.
STARTUP_RETRY_BACKOFF = (5.0, 30.0, 120.0)


# A server that ran this long before it failed earns its retry budget back, so
# its next failure counts as the first. Kubernetes resets a container's restart
# back-off after ten minutes without problems; this is the same rule (audit
# 2026-09-26 C-2, docs/research/2026-09-26-a-revived-server-waits-its-turn.md).
HEALTHY_RUN_SECONDS = 600.0


def _startup_is_retryable(error: BaseException, prefix: str) -> bool:
    """Whether trying this start again could plausibly end differently.

    The clock and the operating system are transient: a cold two-second query
    deadline, a machine under load, a temporary file-system error. Identity,
    protocol and capability failures are not -- they describe the install, and
    the same install will fail the same way.
    """
    if _startup_code(error, prefix).endswith("_startup_timeout"):
        return True
    return isinstance(error, OSError) and not isinstance(error, PermissionError)


_STARTUP_DEGRADING_ERRORS = (
    JsonRpcResponseError,
    OSError,
    ProtocolViolation,
    RuntimeError,
    TimeoutError,
)


@dataclass
class _StartupAttempt:
    """The owner nonce one startup attempt published, if it got that far."""

    owner_nonce: str | None = None


def _cleanup_interruption(cleanup_error: BaseException | None):
    """The interruption travelling inside a cleanup failure, if there is one."""
    if cleanup_error is None:
        return None
    return startup_interruption(cleanup_error)


def _reraise_startup_interruption(
    error: BaseException, interruption: BaseException
) -> None:
    """An interruption always propagates; a wrapped one keeps its context."""
    if interruption is error:
        raise error
    _raise_collected_errors([], prior_error=error)


class SessionStartup:
    """Start, retry and retire the session's language-server process."""

    def _await_startup_locked(self, startup_deadline: float) -> None:
        """Wait out another caller's startup; the caller holds the lock."""
        while self._starting:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0 or not self._condition.wait(remaining):
                raise TimeoutError("Pyright startup did not finish before deadline")

    def _startup_needed_locked(self) -> bool:
        """A start is warranted only for a qualified, idle session."""
        if self._process is not None or self._readiness != "not_ready":
            return False
        return self._identity.qualified

    def _claim_startup_locked(
        self,
    ) -> tuple[StartupCleanupError | None, LspProcess | None] | None:
        """The retained owners to clear first, or None when not to start at all."""
        self._reconcile_process_state_locked()
        if not self._startup_needed_locked():
            return None
        retained = (self._startup_cleanup_error, self._startup_process)
        nothing_retained = retained == (None, None)
        if self._startup_claim_refused_locked(nothing_retained):
            return None
        self._degradation_codes = ()
        self._begin_startup_locked(nothing_retained)
        return retained

    def _startup_claim_refused_locked(self, nothing_retained: bool) -> bool:
        """Whether a fresh attempt is refused because the last one is spent.

        A first attempt, and one that still has a retained owner to clear, are
        never refused. A spent attempt is refused for good unless the failure
        scheduled a retry, and then only until that instant has passed.
        """
        if not nothing_retained:
            return False
        if self._startup_attempted:
            return True
        return time.monotonic() < self._startup_retry_after

    def _begin_startup_locked(self, nothing_retained: bool) -> None:
        """Take the startup claim; a first attempt also records that it happened."""
        self._starting = True
        if nothing_retained:
            self._startup_attempted = True

    def _admit_startup(
        self, startup_deadline: float, bootstrap_timeout_seconds: float
    ) -> tuple[StartupCleanupError | None, LspProcess | None] | None:
        """Claim the right to start, or None when this call must not start one."""
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("Pyright session is closed")
            if self._startup_refused_locked(bootstrap_timeout_seconds):
                return None
            self._await_startup_locked(startup_deadline)
            return self._claim_startup_locked()

    def _startup_refused_locked(self, bootstrap_timeout_seconds: float) -> bool:
        """Whether this session may not start at all; the reason is recorded."""
        if self._capacity_locked:
            self._degrade_locked(self._profile.degradation_code("capacity_exhausted"))
            return True
        if bootstrap_timeout_seconds <= 0:
            self._degrade_locked(self._profile.degradation_code("startup_timeout"))
            return True
        return False

    def _startup_retry_ok(self, action: Callable[[], None]) -> bool:
        """Retry a retained owner; False when it failed in an expected way."""
        try:
            action()
        except BaseException as error:
            interruption = startup_interruption(error)
            if interruption is not None:
                _reraise_startup_interruption(error, interruption)
            if isinstance(error, (OSError, RuntimeError, TimeoutError)):
                return False
            raise
        return True

    def _clear_retained_cleanup(self, retained: StartupCleanupError) -> None:
        with self._lock:
            if self._startup_cleanup_error is retained:
                self._startup_cleanup_error = None
                self._sync_startup_atexit_locked()

    def _clear_retained_process(self, retained: LspProcess) -> None:
        with self._lock:
            if self._startup_process is retained:
                self._startup_process = None
                self._sync_startup_atexit_locked()

    def _clear_retained_owners(
        self,
        retained_cleanup: StartupCleanupError | None,
        retained_process: LspProcess | None,
        startup_deadline: float,
    ) -> bool:
        """Clear what a previous attempt left; False when one refused again."""
        if not self._retained_cleanup_cleared(retained_cleanup, startup_deadline):
            return False
        return self._retained_process_cleared(retained_process, startup_deadline)

    def _retained_cleanup_cleared(
        self, retained_cleanup: StartupCleanupError | None, startup_deadline: float
    ) -> bool:
        if retained_cleanup is None:
            return True
        if not self._startup_retry_ok(
            lambda: retained_cleanup.retry_cleanup(startup_deadline)
        ):
            return False
        self._clear_retained_cleanup(retained_cleanup)
        return True

    def _retained_process_cleared(
        self, retained_process: LspProcess | None, startup_deadline: float
    ) -> bool:
        if retained_process is None:
            return True
        if not self._startup_retry_ok(
            lambda: retained_process.close(startup_deadline)
        ):
            return False
        self._clear_retained_process(retained_process)
        return True

    def _clear_bootstrap_nonce_locked(self, attempt: _StartupAttempt) -> None:
        if self._bootstrap_owner_nonce == attempt.owner_nonce:
            self._bootstrap_owner_nonce = None

    def _clear_bootstrap_nonce(self, attempt: _StartupAttempt) -> None:
        with self._lock:
            self._clear_bootstrap_nonce_locked(attempt)

    def _prepare_owner(
        self, attempt: _StartupAttempt, *, startup_deadline: float
    ) -> tuple[Path, Path, Path]:
        """The node and server paths, and a fresh owner root for this attempt."""
        node, server = self._validated_qualified_paths(deadline=startup_deadline)
        ensure_lsp_parent(self._state_root, deadline=startup_deadline)
        owner = lsp_owner_root(self._state_root, secrets.token_hex(16))
        attempt.owner_nonce = owner.name
        with self._lock:
            self._bootstrap_owner_nonce = owner.name
        return node, server, owner

    def _start_configured_process(
        self,
        node: Path,
        server: Path,
        owner: Path,
        *,
        bootstrap_timeout_seconds: float,
        startup_deadline: float,
    ) -> LspProcess:
        """Start the pinned server under its owner root, with our handlers."""
        command = self._profile.launch_command(node, server, owner)
        return LspProcess.start_configured(
            command,
            cwd=Path(self._repository.checkout_root),
            owner_root=owner,
            deadline=startup_deadline,
            environment_overrides=self._profile.launch_environment(self._state_root),
            server_request_handlers=self._server_request_handlers(),
            server_notification_handlers=self._server_notification_handlers(),
            generation_bootstrap=(
                lambda protocol,
                process_id,
                generation_nonce,
                generation_deadline: self._bootstrap_owned_generation(
                    owner.name,
                    protocol,
                    process_id,
                    generation_nonce,
                    generation_deadline,
                )
            ),
            bootstrap_timeout_seconds=bootstrap_timeout_seconds,
            generation_guard=(
                lambda _generation_nonce, generation_deadline: LaunchServerGuard(
                    server,
                    self._identity.executable_sha256,
                    command=command,
                    owner_root=owner,
                    deadline=generation_deadline,
                    degradation_prefix=self._profile.degradation_prefix,
                    **self._launch_strategy_arguments(),
                )
            ),
        )

    def _launch_strategy_arguments(self) -> dict[str, object]:
        """The launch strategy this profile declares, or nothing at all.

        Passing nothing rather than `package_launch=None` is deliberate: it keeps
        the guard construction for a profile that declares no strategy --
        Pyright -- literally the call it has always been, so the descriptor
        launch that closes the verify-then-execute race cannot be disturbed by a
        change made for a second language.
        """
        launch = self._profile.package_launch
        if self._profile.native:
            return {"native": True}
        if launch is None:
            return {}
        return {"package_launch": launch}

    def _retry_startup_cleanup(
        self, error: BaseException, startup_deadline: float
    ) -> StartupCleanupError | None:
        """Retry an owner cleanup the launch left behind; what still needs keeping."""
        if not isinstance(error, StartupCleanupError):
            return None
        try:
            error.retry_cleanup(
                min(startup_deadline, time.monotonic() + OWNER_CLEANUP_SECONDS)
            )
        except (KeyboardInterrupt, SystemExit):
            with self._lock:
                self._retain_startup_cleanup_locked(error)
            raise
        except BaseException:
            return error
        return None

    def _record_retained_cleanup_locked(
        self, retained_error: StartupCleanupError | None
    ) -> None:
        if retained_error is not None:
            self._retain_startup_cleanup_locked(retained_error)
            return
        self._startup_cleanup_error = None
        self._sync_startup_atexit_locked()

    def _record_retained_cleanup(
        self, retained_error: StartupCleanupError | None
    ) -> None:
        with self._lock:
            self._record_retained_cleanup_locked(retained_error)

    def _allow_startup_retry(self) -> None:
        with self._lock:
            self._startup_attempted = False

    def _record_startup_degradation(
        self, error: BaseException, retained_error: StartupCleanupError | None
    ) -> None:
        """A launch that failed in a known way leaves the session degraded."""
        code = _startup_code(error, self._profile.degradation_prefix)
        with self._lock:
            self._process = None
            self._record_retained_cleanup_locked(retained_error)
            self._degrade_locked(code)
            self._schedule_startup_retry_locked(error)

    def _schedule_startup_retry_locked(self, error: BaseException) -> None:
        """Let a later caller try this start again, once the backoff has passed."""
        if self._startup_retries >= len(STARTUP_RETRY_BACKOFF):
            return
        if not _startup_is_retryable(error, self._profile.degradation_prefix):
            return
        self._startup_retry_after = (
            time.monotonic() + STARTUP_RETRY_BACKOFF[self._startup_retries]
        )
        self._startup_retries += 1
        self._startup_attempted = False

    def _handle_launch_failure(
        self,
        error: BaseException,
        attempt: _StartupAttempt,
        *,
        startup_deadline: float,
    ) -> None:
        """Record a failed launch; returns only when it counts as degradation."""
        self._clear_bootstrap_nonce(attempt)
        interruption = startup_interruption(error)
        retained_error = self._retry_startup_cleanup(error, startup_deadline)
        if interruption is not None:
            self._record_retained_cleanup(retained_error)
            _reraise_startup_interruption(error, interruption)
        self._require_degrading_launch_error(error)
        self._record_startup_degradation(error, retained_error)

    def _require_degrading_launch_error(self, error: BaseException) -> None:
        """Propagate the failures that are not this session's to absorb."""
        if isinstance(error, (TypeError, ValueError)):
            self._allow_startup_retry()
            raise error
        if not isinstance(error, _STARTUP_DEGRADING_ERRORS):
            raise error

    def _launch_server(
        self,
        attempt: _StartupAttempt,
        *,
        bootstrap_timeout_seconds: float,
        startup_deadline: float,
    ) -> LspProcess | None:
        """The started process, or None when the failure was recorded as degraded."""
        try:
            node, server, owner = self._prepare_owner(
                attempt, startup_deadline=startup_deadline
            )
            return self._start_configured_process(
                node,
                server,
                owner,
                bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                startup_deadline=startup_deadline,
            )
        except BaseException as error:
            self._handle_launch_failure(
                error, attempt, startup_deadline=startup_deadline
            )
            return None

    def _reset_after_failed_start(
        self,
        process: LspProcess,
        retained_owner: LspProcess | None,
        attempt: _StartupAttempt,
    ) -> None:
        """Forget everything the failed attempt might have published."""
        with self._lock:
            if self._process is process:
                self._process = None
            self._clear_bootstrap_nonce_locked(attempt)
            self._startup_process = retained_owner
            self._sync_startup_atexit_locked()
            self._reset_readiness_locked()
            self._forget_generation_locked()

    def _close_failed_process(
        self,
        process: LspProcess | None,
        attempt: _StartupAttempt,
        *,
        startup_deadline: float,
    ) -> BaseException | None:
        """Close a process that never became ours; the close error, if any."""
        if process is None:
            return None
        cleanup_deadline = min(
            startup_deadline, time.monotonic() + OWNER_CLEANUP_SECONDS
        )
        retained_owner: LspProcess | None = None
        cleanup_error: BaseException | None = None
        try:
            process.close(cleanup_deadline)
        except BaseException as close_error:
            retained_owner = process
            cleanup_error = close_error
        self._reset_after_failed_start(process, retained_owner, attempt)
        return cleanup_error

    def _abandon_startup(
        self,
        error: BaseException,
        process: LspProcess | None,
        attempt: _StartupAttempt,
        *,
        startup_deadline: float,
    ) -> None:
        """Undo a startup that raised, then propagate what stopped it."""
        cleanup_error = self._close_failed_process(
            process, attempt, startup_deadline=startup_deadline
        )
        interruption = startup_interruption(error) or _cleanup_interruption(
            cleanup_error
        )
        if interruption is None:
            raise error
        self._allow_startup_retry()
        if cleanup_error is not None:
            _raise_collected_errors([cleanup_error], prior_error=error)
        _reraise_startup_interruption(error, interruption)

    def _start_owned(
        self,
        retained: tuple[StartupCleanupError | None, LspProcess | None],
        *,
        bootstrap_timeout_seconds: float,
        startup_deadline: float,
    ) -> None:
        """Start a server while this caller holds the starting flag."""
        retained_cleanup, retained_process = retained
        attempt = _StartupAttempt()
        process: LspProcess | None = None
        try:
            if not self._ready_to_launch(
                retained_cleanup, retained_process, startup_deadline
            ):
                return
            with self._lock:
                self._startup_attempted = True
            process = self._launch_server(
                attempt,
                bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                startup_deadline=startup_deadline,
            )
            if process is None:
                return
            with self._lock:
                self._process = process
                self._process_started_at = time.monotonic()
                self._startup_process = None
                self._sync_startup_atexit_locked()
        except BaseException as error:
            self._abandon_startup(
                error, process, attempt, startup_deadline=startup_deadline
            )

    def _ready_to_launch(
        self,
        retained_cleanup: StartupCleanupError | None,
        retained_process: LspProcess | None,
        startup_deadline: float,
    ) -> bool:
        """What a previous attempt left is cleared, and the backoff has passed."""
        if not self._clear_retained_owners(
            retained_cleanup, retained_process, startup_deadline
        ):
            return False
        return not self._paced_by_backoff()

    def _paced_by_backoff(self) -> bool:
        """A replacement is not launched before the backoff its failure named.

        Clearing what the failed process left runs at once; only the new launch
        waits, and the query answers degraded meanwhile (audit 2026-09-26 C-2).
        """
        with self._lock:
            if time.monotonic() >= self._startup_retry_after:
                return False
            self._degrade_locked(self._profile.degradation_code("startup_retry_pending"))
            return True

    def start(self, *, deadline: float) -> None:
        caller_deadline = validated_deadline(deadline)
        startup_started = time.monotonic()
        startup_deadline = min(caller_deadline, startup_started + STARTUP_SECONDS)
        bootstrap_timeout_seconds = startup_deadline - startup_started
        with self._operation():
            retained = self._admit_startup(
                startup_deadline, bootstrap_timeout_seconds
            )
            if retained is None:
                return
            try:
                self._start_owned(
                    retained,
                    bootstrap_timeout_seconds=bootstrap_timeout_seconds,
                    startup_deadline=startup_deadline,
                )
            finally:
                self._finish_starting()

    def _finish_starting(self) -> None:
        with self._lock:
            self._starting = False
            self._condition.notify_all()

    def _reconcile_process_state_locked(self) -> None:
        if not self._process_state_needs_reconciling():
            return
        self._reset_readiness_locked()
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._workspace_revision = None
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()
        self._retire_failed_process_locked()

    def _retire_failed_process_locked(self) -> None:
        """A process that failed for good is handed to cleanup, so a later start may replace it.

        The session kept the dead process as its own, and `_startup_needed_locked`
        never allowed another start: a key in steady use stayed degraded until the
        server process was restarted by hand (audit B-40,
        docs/research/2026-09-25-a-failed-server-is-started-again.md). The same
        startup retry budget bounds how often that happens, and the same backoff
        paces it: the replacement waits its turn instead of starting at once, and
        a server that ran for `HEALTHY_RUN_SECONDS` first gets the budget back
        (audit 2026-09-26 C-2). A replacement that then fails for an identity,
        protocol or capability reason stays terminal through `_start_owned`.
        """
        process = self._process
        if process is None or process.state is not ProcessState.FAILED:
            return
        self._renew_budget_after_healthy_run_locked()
        if self._startup_retries >= len(STARTUP_RETRY_BACKOFF):
            return
        self._startup_retry_after = time.monotonic() + STARTUP_RETRY_BACKOFF[self._startup_retries]
        self._startup_retries += 1
        self._detach_recovered_process_locked(process, None)
        self._startup_attempted = False

    def _renew_budget_after_healthy_run_locked(self) -> None:
        if time.monotonic() - self._process_started_at >= HEALTHY_RUN_SECONDS:
            self._startup_retries = 0

    def _process_state_needs_reconciling(self) -> bool:
        """The process has failed or degraded out from under this session."""
        process = self._process
        if process is None:
            return False
        if process.state is ProcessState.FAILED:
            return True
        return self._degraded_generation_is_ours(process)

    def _degraded_generation_is_ours(self, process: LspProcess) -> bool:
        """A degraded process only reconciles when the failed generation is ours."""
        if process.state is not ProcessState.DEGRADED:
            return False
        generation = self._generation_nonce
        return generation is None or generation == process.generation_nonce
