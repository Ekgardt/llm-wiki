"""Keep the live language-server sessions of one MCP process within capacity, one per key."""

import atexit
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from interruption import raise_collected_errors as _raise_collected_errors
from llm_wiki.pyright_session_core import LanguageServerSession, PyrightSession
from llm_wiki.pyright_session_launch import released_error
from llm_wiki.pyright_session_values import (
    LOCK_POLL_SECONDS,
    OWNER_CLEANUP_SECONDS,
    validated_deadline,
)
from lsp_identity import discover_managed_server
from lsp_profiles import PYRIGHT_PROFILE
from lsp_server_profile import LanguageServerProfile
from pyright_profile import PyrightIdentity
from repository_scope import RepositoryScope

# Live language servers one MCP process keeps: 4, from the 2026-07-22 Pyright plan; past it an
# idle session is closed to make room or the request degrades. Not measured.
MAX_LSP_PROCESSES = 4
# How long a session may go unused before the next request closes the server it
# owns. The manager keeps one clock: this bound and capacity eviction both read
# the session's own last-used instant, so "idle" means one thing here. See
# `docs/research/2026-09-18-lsp-one-clock-for-one-idea-of-idle.md`.
IDLE_SECONDS = 300.0
# How much of a caller's time one idle reap may spend. There is no daemon, so
# the reap rides on a request; it must not become the request's cost.
_IDLE_REAP_SECONDS = 2.0


class _KeyLockState:
    __slots__ = ("lock", "reference_lock", "references")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reference_lock = threading.Lock()
        self.references = 0


@dataclass(frozen=True)
class _SessionLookup:
    """What one locked attempt at getting a session decided."""

    session: LanguageServerSession | None = None
    wait_for: LanguageServerSession | None = None
    reserved: tuple[object, PyrightSession] | None = None


def _require_get_arguments(
    repository: RepositoryScope, profile: LanguageServerProfile
) -> None:
    if not isinstance(repository, RepositoryScope):
        raise TypeError("repository must be a RepositoryScope")
    if not isinstance(profile, LanguageServerProfile):
        raise TypeError("profile must be a LanguageServerProfile")


def _discovered_identity(
    profile: LanguageServerProfile,
    repository: RepositoryScope,
    state_root: Path,
    deadline: float,
) -> PyrightIdentity:
    """Ask the right discovery for this profile, without installing anything.

    Pyright keeps its own, which accepts a project-local or system candidate and
    re-derives the whole identity for whichever wins. Every other profile is
    managed-root only; `scripts/lsp_identity.py` says why.
    """
    if profile is PYRIGHT_PROFILE:
        from pyright_profile import discover_pyright

        return discover_pyright(repository, state_root=state_root, deadline=deadline)
    return discover_managed_server(
        profile, repository, state_root=state_root, deadline=deadline
    )


class LanguageServerSessionManager:
    """Bound live language-server sessions to four processes per MCP process."""

    def __init__(self, *, state_root: Path) -> None:
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        self._state_root = state_root
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, PyrightIdentity], LanguageServerSession] = {}
        self._key_locks: dict[tuple[str, PyrightIdentity], _KeyLockState] = {}
        self._key_lock_releases: queue.SimpleQueue[
            tuple[tuple[str, PyrightIdentity], _KeyLockState]
        ] = queue.SimpleQueue()
        self._atexit_registered = False
        self._closed = False

    @staticmethod
    def _profile_key(
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile = PYRIGHT_PROFILE,
    ) -> tuple[str, str, PyrightIdentity]:
        """One live process per (checkout, language, identity).

        The profile name is in the key because two languages in one checkout are
        two servers, not one contended session: without it a Python and a
        TypeScript request would collide on the same slot and evict each other.
        """
        return repository.checkout_id, profile.name, identity

    def _register_atexit_locked(self) -> None:
        if not self._atexit_registered:
            atexit.register(self._atexit_close_all)
            self._atexit_registered = True

    def _atexit_close_all(self) -> None:
        try:
            self.close_all(deadline=time.monotonic() + OWNER_CLEANUP_SECONDS)
        except BaseException:
            pass

    def _acquire_manager(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session manager lock deadline expired")

    @staticmethod
    def _acquire_key_lock(lock: threading.Lock, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session key lock deadline expired")

    def _drop_key_lock_reference(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> None:
        """Give back one reference; the last one retires the lock."""
        state.references -= 1
        if state.references < 0:
            raise RuntimeError("Pyright session key lock reference underflow")
        if state.references == 0 and self._key_locks.get(key) is state:
            self._key_locks.pop(key, None)

    def _release_key_lock_reference_once(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> bool:
        """False when another thread holds the reference lock right now."""
        if not state.reference_lock.acquire(blocking=False):
            return False
        try:
            self._drop_key_lock_reference(key, state)
        finally:
            state.reference_lock.release()
        return True

    def _drain_key_lock_releases_locked(self) -> None:
        deferred: list[
            tuple[tuple[str, PyrightIdentity], _KeyLockState]
        ] = []
        while True:
            try:
                key, state = self._key_lock_releases.get_nowait()
            except queue.Empty:
                break
            if not self._release_key_lock_reference_once(key, state):
                deferred.append((key, state))
        for release in deferred:
            self._key_lock_releases.put(release)

    def _prune_one_key_lock(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> None:
        """Retire an unreferenced key lock, if nobody is touching it now."""
        if not state.reference_lock.acquire(blocking=False):
            return
        try:
            self._forget_unreferenced_key_lock(key, state)
        finally:
            state.reference_lock.release()

    def _prune_key_locks_locked(self) -> None:
        self._drain_key_lock_releases_locked()
        for key, state in tuple(self._key_locks.items()):
            self._prune_one_key_lock(key, state)

    def _retain_key_lock_locked(
        self,
        key: tuple[str, PyrightIdentity],
        deadline: float,
    ) -> _KeyLockState:
        self._prune_key_locks_locked()
        state = self._key_locks.get(key)
        if state is None:
            state = _KeyLockState()
            self._key_locks[key] = state
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not state.reference_lock.acquire(timeout=remaining):
            self._forget_unreferenced_key_lock(key, state)
            raise TimeoutError(
                "Pyright session key lock reference deadline expired"
            )
        try:
            state.references += 1
        finally:
            state.reference_lock.release()
        return state

    def _forget_unreferenced_key_lock(
        self, key: tuple[str, PyrightIdentity], state: _KeyLockState
    ) -> None:
        if state.references == 0 and self._key_locks.get(key) is state:
            self._key_locks.pop(key, None)

    def _release_key_lock_reference(
        self,
        key: tuple[str, PyrightIdentity],
        state: _KeyLockState,
    ) -> None:
        self._key_lock_releases.put((key, state))
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._prune_key_locks_locked()
        finally:
            self._lock.release()

    def _key_locks_released(self, deadline: float) -> bool:
        self._acquire_manager(deadline)
        try:
            self._prune_key_locks_locked()
            return not self._key_locks and self._key_lock_releases.empty()
        finally:
            self._lock.release()

    def _wait_for_key_lock_releases(self, deadline: float) -> None:
        while True:
            if self._key_locks_released(deadline):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Pyright session key references did not release before deadline"
                )
            time.sleep(min(LOCK_POLL_SECONDS, remaining))

    @staticmethod
    def _session_state(
        session: LanguageServerSession,
        deadline: float,
    ) -> tuple[bool, bool, bool, int, float]:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not session._lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session state lock deadline expired")
        try:
            return (
                session._closed,
                session._closing,
                session._starting,
                session._active_operations,
                session._last_used_monotonic,
            )
        finally:
            session._lock.release()

    def _live_entries_locked(
        self,
        deadline: float,
    ) -> list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]]:
        live: list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]] = []
        for key, session in tuple(self._sessions.items()):
            closed, _closing, _starting, _active, _last_used = self._session_state(
                session,
                deadline,
            )
            if closed:
                self._forget_session_locked(key, session)
                continue
            live.append((key, session))
        return live

    @staticmethod
    def _eviction_rank(session: LanguageServerSession, deadline: float) -> int:
        """0 for a session that owns no server; it costs a caller nothing.

        A checkout whose start failed keeps answering `get()`, and each answer
        renews its last-used time, so least-recently-used alone would evict a
        healthy neighbour to keep a session that owns nothing.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not session._lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session state lock deadline expired")
        try:
            return 1 if session._process is not None else 0
        finally:
            session._lock.release()

    def _idle_entry(
        self,
        key: tuple[str, PyrightIdentity],
        session: LanguageServerSession,
        deadline: float,
    ) -> tuple[int, float, tuple[str, PyrightIdentity], LanguageServerSession] | None:
        """The session's eviction order, when it is idle enough to evict."""
        closed, closing, starting, active, last_used = self._session_state(
            session,
            deadline,
        )
        if closed or closing or starting:
            return None
        if active != 0:
            return None
        return self._eviction_rank(session, deadline), last_used, key, session

    def _reserve_lru_idle_locked(
        self,
        live: list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]],
        deadline: float,
    ) -> tuple[tuple[str, PyrightIdentity], LanguageServerSession] | None:
        idle: list[
            tuple[int, float, tuple[str, PyrightIdentity], LanguageServerSession]
        ] = []
        for key, session in live:
            entry = self._idle_entry(key, session, deadline)
            if entry is not None:
                idle.append(entry)
        for _rank, _last_used, key, session in sorted(idle, key=lambda item: item[:2]):
            if session._reserve_idle_close(deadline):
                return key, session
        return None

    @staticmethod
    def _finish_session_close(session: LanguageServerSession, deadline: float) -> None:
        """Close a session found closing, under this caller's deadline.

        `close` is serialized: while another caller is closing, this waits for
        it and then finds nothing left to do. When the earlier close failed and
        nobody is closing, this is the retry -- polling the flag, as this did
        before 2026-09-17, waited for a close that was never coming.
        """
        session.close(deadline=deadline)

    def _stranded_entry_locked(
        self,
        live: list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]],
        deadline: float,
    ) -> tuple[tuple[str, PyrightIdentity], LanguageServerSession] | None:
        """A session whose close failed: it holds a slot and serves nobody."""
        for key, session in live:
            if self._close_is_stranded(session, deadline):
                return key, session
        return None

    @staticmethod
    def _close_is_stranded(session: LanguageServerSession, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not session._lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright session state lock deadline expired")
        try:
            return not session._closed and session._close_stranded
        finally:
            session._lock.release()

    def _idle_expired_session(
        self,
        key: tuple[str, PyrightIdentity],
        session: LanguageServerSession,
        now: float,
        deadline: float,
    ) -> bool:
        """Whether the server this session owns has gone unused past the limit.

        The clock is the session's own last-used instant, the one capacity
        eviction already orders by: one idea of "idle" in one place. A session
        that owns no server is rank 0 and costs a caller nothing, so there is
        nothing in it to reap.
        """
        entry = self._idle_entry(key, session, deadline)
        if entry is None:
            return False
        rank, last_used, _key, _session = entry
        if rank == 0:
            return False
        return now - last_used >= IDLE_SECONDS

    def _reserve_if_idle_expired(
        self,
        key: tuple[str, PyrightIdentity],
        session: LanguageServerSession,
        now: float,
        deadline: float,
    ) -> bool:
        if not self._idle_expired_session(key, session, now, deadline):
            return False
        return session._reserve_idle_close(deadline)

    def _first_reservable_idle(
        self,
        key: object,
        live: list[tuple[tuple[str, PyrightIdentity], LanguageServerSession]],
        now: float,
        deadline: float,
    ) -> tuple[object, LanguageServerSession] | None:
        for entry_key, session in live:
            if entry_key == key:
                continue
            if self._reserve_if_idle_expired(entry_key, session, now, deadline):
                return entry_key, session
        return None

    def _reserve_idle_expired(
        self, key: object, deadline: float
    ) -> tuple[object, LanguageServerSession] | None:
        """One server unused past the idle limit, reserved for closing."""
        now = time.monotonic()
        self._acquire_manager(deadline)
        try:
            live = self._live_entries_locked(deadline)
        finally:
            self._lock.release()
        return self._first_reservable_idle(key, live, now, deadline)

    def _reap_idle_sessions(self, key: object, deadline: float) -> None:
        """Close one server left unused past the idle limit, on this caller's time.

        There is no daemon, so a request is the only clock the product has. The
        reap is bounded, it never touches the session being asked for, and it
        never fails the caller: a close that does not finish leaves a stranded
        session, which the next caller for that key retries.
        """
        reap_deadline = min(deadline, time.monotonic() + _IDLE_REAP_SECONDS)
        try:
            reserved = self._reserve_idle_expired(key, reap_deadline)
        except TimeoutError:
            return
        if reserved is None:
            return
        self._close_reaped(reserved[1], reap_deadline)

    @staticmethod
    def _close_reaped(session: LanguageServerSession, deadline: float) -> None:
        try:
            session.close(deadline=deadline)
        except (OSError, RuntimeError, TimeoutError):
            return

    def _admit_manager_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Pyright session manager is closed")

    def _forget_session_locked(self, key: object, session: LanguageServerSession) -> None:
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)

    def _new_session_locked(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
    ) -> LanguageServerSession:
        session = LanguageServerSession(
            repository, identity, state_root=self._state_root, profile=profile
        )
        self._sessions[key] = session
        self._register_atexit_locked()
        return session

    def _capacity_denied_session(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
    ) -> LanguageServerSession:
        """A session that refuses to start because the manager is at capacity."""
        denied = LanguageServerSession(
            repository, identity, state_root=self._state_root, profile=profile
        )
        denied._capacity_locked = True
        return denied

    def _existing_session_locked(
        self, key: object, deadline: float
    ) -> _SessionLookup | None:
        """What to do about a session already registered under this key."""
        existing = self._sessions.get(key)
        if existing is None:
            return None
        closed, closing, _starting, _active, _last_used = self._session_state(
            existing, deadline
        )
        if closed:
            self._forget_session_locked(key, existing)
            return None
        return _SessionLookup(wait_for=existing) if closing else _SessionLookup(
            session=existing
        )

    def _lookup_session_locked(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> _SessionLookup:
        """One locked attempt: a session, one to wait for, or one to evict."""
        self._admit_manager_locked()
        existing = self._existing_session_locked(key, deadline)
        if existing is not None:
            return existing
        live = self._live_entries_locked(deadline)
        if len(live) < MAX_LSP_PROCESSES:
            return _SessionLookup(
                session=self._new_session_locked(key, repository, identity, profile)
            )
        return self._crowded_lookup_locked(live, repository, identity, profile, deadline)

    def _crowded_lookup_locked(
        self,
        live: list,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> _SessionLookup:
        """At capacity: evict the least recently used idle session, or refuse."""
        reserved = self._stranded_entry_locked(
            live, deadline
        ) or self._reserve_lru_idle_locked(live, deadline)
        if reserved is None:
            return _SessionLookup(
                session=self._capacity_denied_session(repository, identity, profile)
            )
        return _SessionLookup(reserved=reserved)

    def _lookup_session(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> _SessionLookup:
        self._acquire_manager(deadline)
        try:
            return self._lookup_session_locked(
                key, repository, identity, profile, deadline
            )
        finally:
            self._lock.release()

    def _adopt_after_eviction_locked(
        self,
        evicted_key: object,
        evicted: LanguageServerSession,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | None:
        """The session replacing the evicted one, or None to look again."""
        closed, _closing, _starting, _active, _last_used = self._session_state(
            evicted, deadline
        )
        if not closed:
            raise RuntimeError("eviction close did not release the session")
        self._forget_session_locked(evicted_key, evicted)
        self._admit_manager_locked()
        live = self._live_entries_locked(deadline)
        if len(live) >= MAX_LSP_PROCESSES:
            return None
        return self._new_session_locked(key, repository, identity, profile)

    def _evict_and_adopt(
        self,
        reserved: tuple[object, LanguageServerSession],
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | None:
        """Close the reserved session, then take its place if one is free."""
        evicted_key, evicted = reserved
        evicted.close(deadline=deadline)
        self._acquire_manager(deadline)
        try:
            return self._adopt_after_eviction_locked(
                evicted_key, evicted, key, repository, identity, profile, deadline
            )
        finally:
            self._lock.release()

    def _session_for_key(
        self,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession:
        """The session for this key, waiting or evicting until there is one."""
        while True:
            lookup = self._lookup_session(key, repository, identity, profile, deadline)
            if lookup.session is not None:
                return lookup.session
            adopted = self._advance_lookup(
                lookup, key, repository, identity, profile, deadline
            )
            if adopted is not None:
                return adopted

    def _advance_lookup(
        self,
        lookup: _SessionLookup,
        key: object,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | None:
        """Finish a closing session, or evict a reserved one; None to look again."""
        if lookup.wait_for is not None:
            self._finish_session_close(lookup.wait_for, deadline)
            return None
        assert lookup.reserved is not None
        return self._evict_and_adopt(
            lookup.reserved, key, repository, identity, profile, deadline
        )

    def _admit_get(
        self,
        repository: RepositoryScope,
        identity: PyrightIdentity,
        profile: LanguageServerProfile,
        deadline: float,
    ) -> LanguageServerSession | tuple[object, object]:
        """The key and its lock, or an unqualified session that needs neither."""
        self._acquire_manager(deadline)
        try:
            self._admit_manager_locked()
            if not identity.qualified:
                return LanguageServerSession(
                    repository, identity, state_root=self._state_root, profile=profile
                )
            key = self._profile_key(repository, identity, profile)
            return key, self._retain_key_lock_locked(key, deadline)
        finally:
            self._lock.release()

    def get(
        self,
        repository: RepositoryScope,
        *,
        deadline: float,
        profile: LanguageServerProfile = PYRIGHT_PROFILE,
    ) -> LanguageServerSession:
        deadline = validated_deadline(deadline)
        _require_get_arguments(repository, profile)
        identity = _discovered_identity(profile, repository, self._state_root, deadline)
        admitted = self._admit_get(repository, identity, profile, deadline)
        if isinstance(admitted, LanguageServerSession):
            return admitted
        key, key_lock_state = admitted
        key_lock_acquired = False
        try:
            self._acquire_key_lock(key_lock_state.lock, deadline)
            key_lock_acquired = True
            self._reap_idle_sessions(key, deadline)
            return self._session_for_key(
                key, repository, identity, profile, deadline
            )
        finally:
            if key_lock_acquired:
                key_lock_state.lock.release()
            self._release_key_lock_reference(key, key_lock_state)

    def close_all(self, *, deadline: float) -> None:
        deadline = validated_deadline(deadline)
        self._acquire_manager(deadline)
        try:
            self._closed = True
            self._prune_key_locks_locked()
            sessions = tuple(self._sessions.items())
        finally:
            self._lock.release()

        errors: list[BaseException] = []
        for key, session in sessions:
            error = self._close_one_session(key, session, deadline)
            if error is not None:
                errors.append(error)
        error = released_error(
            lambda: self._wait_for_key_lock_releases(deadline)
        )
        if error is not None:
            errors.append(error)
        _raise_collected_errors(errors)

    def _forget_closed_session(
        self, key: object, session: LanguageServerSession, deadline: float
    ) -> None:
        """Drop a session we just closed, refusing one that did not release."""
        self._acquire_manager(deadline)
        try:
            closed, _closing, _starting, _active, _last_used = self._session_state(
                session, deadline
            )
            if not closed:
                raise RuntimeError(
                    "Pyright close_all close did not release the session"
                )
            self._forget_session_locked(key, session)
        finally:
            self._lock.release()

    def _close_one_session(
        self, key: object, session: LanguageServerSession, deadline: float
    ) -> BaseException | None:
        """Close one session and forget it; the error worth reporting, if any."""
        error = released_error(lambda: session.close(deadline=deadline))
        if error is not None:
            return error
        return released_error(
            lambda: self._forget_closed_session(key, session, deadline)
        )


# The manager name `scripts/mcp_server.py` imports. Same object.
PyrightSessionManager = LanguageServerSessionManager
