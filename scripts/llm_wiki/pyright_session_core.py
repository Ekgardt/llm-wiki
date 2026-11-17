"""The language-server session composed from its concern parts, with its state, locks and handlers."""

import atexit
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_bootstrap import SessionBootstrap
from llm_wiki.pyright_session_calls import SessionCalls
from llm_wiki.pyright_session_close import SessionClose
from llm_wiki.pyright_session_diagnostics import SessionDiagnostics
from llm_wiki.pyright_session_documents import SessionDocuments
from llm_wiki.pyright_session_progress import SessionProgress
from llm_wiki.pyright_session_queries import SessionQueries
from llm_wiki.pyright_session_recovery import SessionRecovery
from llm_wiki.pyright_session_startup import SessionStartup
from llm_wiki.pyright_session_values import (
    OWNER_CLEANUP_SECONDS,
    DiagnosticSnapshot,
    OpenDocument,
    validated_deadline,
)
from llm_wiki.pyright_session_wire import SessionWire
from lsp_process import LspProcess, StartupCleanupError
from lsp_profiles import PYRIGHT_PROFILE
from lsp_server_profile import LanguageServerProfile
from pyright_profile import PyrightIdentity
from repository_scope import RepositoryScope
from workspace_revision import WorkspaceRevision


def _progress_handler(
    session: LanguageServerSession, method: str
) -> Callable[[object], None]:
    """One notification handler bound to the progress method it reports."""
    return lambda params: session._progress(method, params)


def _require_session_arguments(
    repository: object,
    identity: object,
    state_root: object,
    profile: object,
) -> None:
    """Everything a session is built from, checked before anything is retained."""
    for value, expected, label in (
        (repository, RepositoryScope, "repository"),
        (identity, PyrightIdentity, "identity"),
        (state_root, Path, "state_root"),
        (profile, LanguageServerProfile, "profile"),
    ):
        if not isinstance(value, expected):
            raise TypeError(f"{label} must be a {expected.__name__}")


class LanguageServerSession(
    SessionProgress,
    SessionWire,
    SessionDiagnostics,
    SessionBootstrap,
    SessionStartup,
    SessionDocuments,
    SessionQueries,
    SessionCalls,
    SessionRecovery,
    SessionClose,
):
    """Own one repository-scoped language-server protocol lifecycle.

    One class, one `profile` field -- not a base class with a subclass per
    language. Measurement (`docs/research/2026-08-28-precise-navigation-beyond-python.md`)
    put 171 Pyright-mentioning lines inside 5,028: the other 4,857 are one
    implementation of containment, leases, generations and wire state, and a
    base/subclass split would invite a second override of exactly those.
    Everything language-shaped is read from `self._profile`.

    `PyrightSession` remains as an alias below, because that is what the
    existing Pyright tests and `code_navigation` type-check against.
    """

    def __init__(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        *,
        state_root: Path,
        profile: LanguageServerProfile = PYRIGHT_PROFILE,
    ) -> None:
        _require_session_arguments(repository, identity, state_root, profile)
        self._repository = repository
        self._identity = identity
        self._profile = profile
        self._state_root = state_root
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._document_lock = threading.RLock()
        self._wire_condition = threading.Condition(threading.Lock())
        self._readiness = "not_ready"
        self._readiness_evidence: tuple[str, ...] = ()
        self._position_encoding = None
        self._degradation_codes = identity.degradation_codes
        self._capabilities: dict[str, bool] = {}
        self._active_operations = 0
        self._last_used_monotonic = time.monotonic()
        self._process: LspProcess | None = None
        self._documents: dict[str, OpenDocument] = {}
        self._document_bytes = 0
        # What each open document's file looked like when its bytes were last
        # confirmed against the workspace revision. See `_file_identity`.
        self._document_identities: dict[str, tuple[int, int, int, int]] = {}
        # Use order, for eviction only: a counter per URI, highest used last.
        self._document_use = 0
        self._document_used: dict[str, int] = {}
        self._workspace_revision: WorkspaceRevision | None = None
        self._synchronize_epoch = 0
        self._readiness_target_uri: str | None = None
        self._generation_nonce: str | None = None
        self._ready_uri_generations: dict[str, str] = {}
        self._diagnostics: dict[str, DiagnosticSnapshot] = {}
        self._diagnostic_bytes = 0
        self._progress_events: list[tuple[object, ...]] = []
        self._progress_bytes = 0
        # The work-done tokens the server asked us to create, and the one
        # generation whose project load has been declared finished. Only the
        # profiles that gate on progress consult these.
        self._work_done_tokens: set[object] = set()
        self._progress_ready_generation: str | None = None
        self._wire_generation: str | None = None
        self._wire_opened: set[tuple[str, str, int]] = set()
        self._wire_failed: set[tuple[str, str, int]] = set()
        self._wire_sending: set[tuple[str, str, int]] = set()
        self._starting = False
        self._startup_attempted = False
        # A start that ran out of time, or met the operating system, may be
        # tried again -- at most `len(STARTUP_RETRY_BACKOFF)` times, and never
        # before the monotonic instant the last failure named.
        self._startup_retries = 0
        self._startup_retry_after = 0.0
        self._process_started_at = 0.0
        self._startup_cleanup_error: StartupCleanupError | None = None
        self._startup_process: LspProcess | None = None
        self._bootstrap_owner_nonce: str | None = None
        self._bootstrap_generation_owners: dict[str, str] = {}
        self._startup_atexit_registered = False
        self._closing = False
        self._closed = False
        self._close_stranded = False
        self._capacity_locked = False

    @property
    def identity(self) -> PyrightIdentity:
        return self._identity

    @property
    def profile(self) -> LanguageServerProfile:
        """The pinned managed server this session drives."""
        return self._profile

    @property
    def readiness(self) -> str:
        with self._lock:
            self._reconcile_process_state_locked()
            return self._readiness

    @property
    def readiness_evidence(self) -> tuple[str, ...]:
        with self._lock:
            self._reconcile_process_state_locked()
            return self._readiness_evidence

    @property
    def position_encoding(self) -> PositionEncoding | None:
        with self._lock:
            self._reconcile_process_state_locked()
            return self._position_encoding

    @property
    def degradation_codes(self) -> tuple[str, ...]:
        with self._lock:
            return self._degradation_codes

    @property
    def capabilities(self) -> Mapping[str, bool]:
        with self._lock:
            self._reconcile_process_state_locked()
            return MappingProxyType(dict(self._capabilities))

    @property
    def active_operations(self) -> int:
        with self._lock:
            return self._active_operations

    @property
    def last_used_monotonic(self) -> float:
        with self._lock:
            return self._last_used_monotonic

    @property
    def progress_events(self) -> tuple[tuple[object, ...], ...]:
        with self._lock:
            return tuple(self._progress_events)

    def _sync_startup_atexit_locked(self) -> None:
        """The exit hook is registered exactly while something is retained."""
        retained = (
            self._startup_cleanup_error is not None
            or self._startup_process is not None
        )
        if retained == self._startup_atexit_registered:
            return
        if retained:
            atexit.register(self._atexit_cleanup)
        else:
            atexit.unregister(self._atexit_cleanup)
        self._startup_atexit_registered = retained

    def _retain_startup_cleanup_locked(self, error: StartupCleanupError) -> None:
        self._startup_cleanup_error = error
        self._sync_startup_atexit_locked()
        error.transfer_cleanup_ownership()

    def _atexit_cleanup(self) -> None:
        try:
            self.close(deadline=time.monotonic() + OWNER_CLEANUP_SECONDS)
        except BaseException:
            pass

    def _acquire_state_lock(self, deadline: float, message: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise TimeoutError(message)

    @contextmanager
    def _document_operation_lock(self, deadline: float) -> Iterator[None]:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._document_lock.acquire(timeout=remaining):
                raise TimeoutError("Pyright document lock deadline expired")
            yield
        finally:
            try:
                self._document_lock.release()
            except RuntimeError:
                pass

    @contextmanager
    def _operation(self) -> Iterator[None]:
        now = time.monotonic()
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("Pyright session is closing")
            self._active_operations += 1
            self._last_used_monotonic = now
        try:
            yield
        finally:
            with self._lock:
                self._active_operations -= 1
                self._last_used_monotonic = time.monotonic()
                self._condition.notify_all()

    @contextmanager
    def _synchronize_semantic_fence(self) -> Iterator[None]:
        with self._lock:
            if self._synchronize_epoch % 2 != 0:
                raise RuntimeError("Pyright synchronization fence is already active")
            self._synchronize_epoch += 1
            active_epoch = self._synchronize_epoch
        try:
            yield
        finally:
            self._release_semantic_fence(active_epoch)

    def _release_semantic_fence(self, active_epoch: int) -> None:
        with self._lock:
            if self._synchronize_epoch != active_epoch:
                raise RuntimeError("Pyright synchronization fence changed unexpectedly")
            self._synchronize_epoch += 1
            self._condition.notify_all()

    def _reserve_idle_close(self, deadline: float) -> bool:
        deadline = validated_deadline(deadline)
        self._acquire_state_lock(
            deadline,
            "Pyright session reservation state lock deadline expired",
        )
        try:
            if not self._is_idle_locked():
                return False
            self._closing = True
            self._condition.notify_all()
            return True
        finally:
            self._lock.release()

    def _is_idle_locked(self) -> bool:
        """Nothing is using this session and nothing is starting or stopping it."""
        if self._closed or self._closing or self._starting:
            return False
        return self._active_operations == 0

    def _server_request_handlers(self) -> dict[str, object]:
        return {
            "client/registerCapability": self._benign_server_request,
            "client/unregisterCapability": self._benign_server_request,
            "window/workDoneProgress/create": self._work_done_progress_create,
            "workspace/configuration": self._configuration,
        }

    def _server_notification_handlers(self) -> dict[str, object]:
        handlers: dict[str, object] = {
            method: _progress_handler(self, method)
            for method in self._progress_methods()
        }
        handlers["textDocument/publishDiagnostics"] = self._publish_diagnostics
        self._add_identity_handler(handlers)
        return handlers

    def _add_identity_handler(self, handlers: dict[str, object]) -> None:
        """Register the post-initialize identity assertion, where a profile has one.

        Two checks, not one. `lsp_identity` digests the pinned engine at
        `tsserver.path` against the install receipt *before* the process
        starts, so the file the server is pointed at is known to be ours. This
        is the server's own word, afterwards, that it used that file rather
        than one it found for itself. Since 2026-09-17 the transport's
        allowlist is derived from the profile registry, so the notification
        reaches here instead of being dropped with one warning.
        """
        identity = self._profile.identity_notification
        if identity is None:
            return
        handlers[identity.method] = self._record_server_identity

    def _record_server_identity(self, params: object) -> None:
        identity = self._profile.identity_notification
        if identity is None:
            return
        version, confirmed = identity.confirmed(params)
        self._retain_progress((identity.method, str(version), str(confirmed)))
        self._record_identity_confirmation(confirmed)

    def _record_identity_confirmation(self, confirmed: bool) -> None:
        """A server that did not name the engine we pinned answers as degraded.

        Readiness is left alone: the session still answers, and the caller is
        told the answers come from an engine we did not verify.
        """
        if confirmed:
            return
        code = self._profile.degradation_code("server_identity_unconfirmed")
        with self._lock:
            self._degradation_codes = tuple(
                sorted({*self._degradation_codes, code})
            )


# The name the Pyright tests, `code_navigation` and the MCP server type-check
# against. One class drives every profile; keeping the alias leaves the Python
# path's own vocabulary intact rather than rewriting 5,000 lines for a second
# language. It lives beside the class it names; the manager part imports it from here.
PyrightSession = LanguageServerSession
