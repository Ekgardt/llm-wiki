from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import cast

import pytest
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_manager import (
    MAX_LSP_PROCESSES,
    LanguageServerSessionManager,
    _KeyLockState,
)
from lsp_profiles import PYRIGHT_PROFILE, TYPESCRIPT_PROFILE
from lsp_server_profile import LanguageServerProfile
from pyright_profile import PyrightIdentity
from repository_scope import RepositoryScope, resolve_repository_scope

from tests.language_server_session_state import unstarted_session
from tests.pyright_session_helpers import _missing_identity
from tests.slow_machine import LONG_TIMEOUT, PAUSE_TIMEOUT, SHORT_TIMEOUT

_KEY = ("checkout", _missing_identity())
_NEW_KEY = ("new", _missing_identity())


@pytest.fixture
def manager(state_root: Path) -> LanguageServerSessionManager:
    return LanguageServerSessionManager(state_root=state_root)


@pytest.fixture
def scope(repository: Path) -> RepositoryScope:
    return resolve_repository_scope(repository)


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


def _deadline() -> float:
    return time.monotonic() + SHORT_TIMEOUT


def _expired() -> float:
    return time.monotonic() - 1.0


def _refuse_close_all(*, deadline: float) -> None:
    raise RuntimeError("close_all refused")


def _refuse_session_close(*, deadline: float) -> None:
    raise RuntimeError("close refused")


def _never_released(deadline: float) -> bool:
    return False


def test_a_manager_needs_a_path_for_its_state_root() -> None:
    with pytest.raises(TypeError, match="state_root must be a Path"):
        LanguageServerSessionManager(state_root=cast(Path, "state"))


def test_a_get_for_something_that_is_not_a_repository_scope_is_refused(
    manager: LanguageServerSessionManager,
) -> None:
    with pytest.raises(TypeError, match="repository must be a RepositoryScope"):
        manager.get(cast(RepositoryScope, "repo"), deadline=_deadline())


def test_a_get_for_something_that_is_not_a_profile_is_refused(
    manager: LanguageServerSessionManager, scope: RepositoryScope
) -> None:
    with pytest.raises(TypeError, match="profile must be a LanguageServerProfile"):
        manager.get(scope, deadline=_deadline(), profile=cast(LanguageServerProfile, "x"))


def test_an_uninstalled_managed_profile_gets_an_unkept_unqualified_session(
    manager: LanguageServerSessionManager, scope: RepositoryScope
) -> None:
    session = manager.get(scope, deadline=_deadline(), profile=TYPESCRIPT_PROFILE)

    assert (session.profile, session._identity.qualified, manager._sessions) == (
        TYPESCRIPT_PROFILE,
        False,
        {},
    )


def test_the_exit_hook_closes_the_manager(manager: LanguageServerSessionManager) -> None:
    manager._atexit_close_all()

    assert manager._closed is True


def test_the_exit_hook_never_raises_when_closing_fails(
    manager: LanguageServerSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager, "close_all", _refuse_close_all)

    manager._atexit_close_all()

    assert manager._closed is False


def test_giving_back_a_reference_never_taken_is_refused(
    manager: LanguageServerSessionManager,
) -> None:
    with pytest.raises(RuntimeError, match="reference underflow"):
        manager._drop_key_lock_reference(_KEY, _KeyLockState())


def test_an_unreferenced_key_lock_is_forgotten(manager: LanguageServerSessionManager) -> None:
    state = _KeyLockState()
    manager._key_locks[_KEY] = state

    manager._forget_unreferenced_key_lock(_KEY, state)

    assert manager._key_locks == {}


def test_a_release_while_another_thread_holds_the_manager_is_queued(
    manager: LanguageServerSessionManager,
) -> None:
    state = _KeyLockState()
    state.references = 1
    manager._key_locks[_KEY] = state
    held = threading.Event()
    done = threading.Event()

    def hold_manager() -> None:
        with manager._lock:
            held.set()
            done.wait(PAUSE_TIMEOUT)

    holder = threading.Thread(target=hold_manager)
    holder.start()
    assert held.wait(LONG_TIMEOUT)
    manager._release_key_lock_reference(_KEY, state)
    queued = (state.references, manager._key_lock_releases.qsize())
    done.set()
    holder.join(SHORT_TIMEOUT)

    assert queued == (1, 1)


def test_key_references_that_never_release_end_at_the_deadline(
    manager: LanguageServerSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager, "_key_locks_released", _never_released)

    with pytest.raises(TimeoutError, match="did not release before deadline"):
        manager._wait_for_key_lock_releases(_expired())


def test_reading_a_session_after_the_deadline_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TimeoutError, match="state lock deadline expired"):
        LanguageServerSessionManager._session_state(session, _expired())


def test_ranking_a_session_after_the_deadline_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TimeoutError, match="state lock deadline expired"):
        LanguageServerSessionManager._eviction_rank(session, _expired())


def test_checking_a_stranded_close_after_the_deadline_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TimeoutError, match="state lock deadline expired"):
        LanguageServerSessionManager._close_is_stranded(session, _expired())


def test_reaping_after_the_deadline_leaves_every_session_alone(
    manager: LanguageServerSessionManager, session: LanguageServerSession
) -> None:
    manager._sessions[_KEY] = session

    manager._reap_idle_sessions(("other",), _expired())

    assert (manager._sessions, session._closed) == ({_KEY: session}, False)


def test_a_reaped_session_whose_close_fails_never_fails_the_caller(
    session: LanguageServerSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "close", _refuse_session_close)

    LanguageServerSessionManager._close_reaped(session, _deadline())

    assert session._closed is False


def _adopt(
    manager: LanguageServerSessionManager,
    evicted: LanguageServerSession,
    scope: RepositoryScope,
) -> LanguageServerSession | None:
    identity: PyrightIdentity = _missing_identity()
    with manager._lock:
        return manager._adopt_after_eviction_locked(
            _KEY, evicted, _NEW_KEY, scope, identity, PYRIGHT_PROFILE, _deadline()
        )


def test_an_eviction_whose_close_did_not_release_is_refused(
    manager: LanguageServerSessionManager, session: LanguageServerSession, scope: RepositoryScope
) -> None:
    with pytest.raises(RuntimeError, match="eviction close did not release the session"):
        _adopt(manager, session, scope)


def test_an_eviction_whose_slot_was_taken_meanwhile_looks_again(
    manager: LanguageServerSessionManager,
    session: LanguageServerSession,
    scope: RepositoryScope,
    repository: Path,
    state_root: Path,
) -> None:
    for index in range(MAX_LSP_PROCESSES):
        live = unstarted_session(repository, state_root)
        manager._sessions[(f"live-{index}", _missing_identity())] = live
    session._closed = True

    assert _adopt(manager, session, scope) is None
    assert _NEW_KEY not in manager._sessions


def test_closing_all_refuses_a_session_that_did_not_release(
    manager: LanguageServerSessionManager, session: LanguageServerSession
) -> None:
    manager._sessions[_KEY] = session

    with pytest.raises(RuntimeError, match="close_all close did not release the session"):
        manager._forget_closed_session(_KEY, session, _deadline())

    assert manager._sessions == {_KEY: session}
