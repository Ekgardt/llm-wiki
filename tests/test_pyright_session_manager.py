"""The Pyright session manager: keys, reuse, eviction, capacity, locks and close."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import lsp_process
import pytest
from llm_wiki.pyright_session_core import PyrightSession
from llm_wiki.pyright_session_manager import MAX_LSP_PROCESSES, PyrightSessionManager
from pyright_profile import (
    PyrightIdentity,
)
from repository_scope import RepositoryScope, resolve_repository_scope

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_python_repository,
    create_semantic_pyright_fixture,
)
from tests.pyright_session_helpers import (
    CONTENTION_HOLD_SECONDS,
    CONTENTION_RETURN_SECONDS,
    _join_all,
    _missing_identity,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def _assert_evictions_reserved(manager, evicting) -> None:
    """Capacity stays full while the two reserved sessions refuse new work."""
    with manager._lock:
        assert len(manager._sessions) == MAX_LSP_PROCESSES
    for session in evicting:
        assert session._closing is True
        _assert_operations_refused(session)


def _all_retained(manager, sessions) -> bool:
    return all(session in manager._sessions.values() for session in sessions)


def _stagger_last_used(sessions) -> None:
    """Give the sessions a deterministic idle order for eviction."""
    for index, session in enumerate(sessions):
        with session._lock:
            session._last_used_monotonic = float(index)


def _patch_blocked_closes(patch, sessions, blocked_close) -> None:
    for index, session in enumerate(sessions):
        patch.setattr(
            session,
            "close",
            lambda *, deadline, index=index: blocked_close(index, deadline=deadline),
        )


def _prepared_scopes(tmp_path: Path, monkeypatch, prefix: str, count: int):
    """Build N repositories and patch discovery to their fixture identities."""
    scopes: list[RepositoryScope] = []
    identities: dict[str, object] = {}
    for index in range(count):
        repo, fixture = _make_repo_fixture(tmp_path, f"{prefix}-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
    _patch_discovery(monkeypatch, identities)
    return scopes


def _started_threads(target, arguments):
    threads = [threading.Thread(target=target, args=(item,)) for item in arguments]
    for thread in threads:
        thread.start()
    return threads


def _assert_operations_refused(session) -> None:
    with pytest.raises(RuntimeError, match="closing"):
        with session._operation():
            pytest.fail("operation started after eviction reservation")


def _wait_for_release(release, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not release.wait(remaining):
        raise TimeoutError("parallel eviction release expired")


def _manager_is_closed(manager) -> bool:
    with manager._lock:
        return manager._closed


def _await_manager_closed(manager, deadline: float) -> None:
    while not _manager_is_closed(manager):
        if time.monotonic() >= deadline:
            pytest.fail("close_all did not close manager before key waiter release")
        time.sleep(0.01)


def _probe_lock_from_thread(lock, observed, deadline: float) -> None:
    """Prove the manager lock is free while a close runs outside it."""
    def probe() -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0 and lock.acquire(timeout=remaining):
            observed.set()
            lock.release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))


def _expect_failed_eviction_close(monkeypatch, manager, evicted, fail_close, scope) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(evicted, "close", fail_close)
        with pytest.raises(OSError, match="injected close failure"):
            manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)


def _release_active_operations(sessions) -> None:
    for session in sessions:
        with session._lock:
            session._active_operations = 0
            session._condition.notify_all()


def _run_manager_operation(manager, scope, operation: str, *, deadline: float) -> None:
    if operation == "get":
        manager.get(scope, deadline=deadline)
        return
    manager.close_all(deadline=deadline)


def _join_started_threads(workers) -> None:
    for worker in workers:
        if worker.ident is not None:
            worker.join(SHORT_TIMEOUT)


def _sessions_for_checkout(manager, checkout_id: str) -> list:
    return [
        session
        for session in manager._sessions.values()
        if session._repository.checkout_id == checkout_id
    ]


def _patch_discovery(
    monkeypatch: pytest.MonkeyPatch,
    identities: Mapping[str, object],
) -> None:
    import pyright_profile

    def _fake_discover(repository, *, state_root, deadline=None, **kwargs):
        return identities[repository.checkout_id]

    monkeypatch.setattr(pyright_profile, "discover_pyright", _fake_discover)


def _make_repo_fixture(tmp_path: Path, name: str) -> tuple[Path, SemanticPyrightFixture]:
    repo = create_python_repository(tmp_path / name)
    fixture = create_semantic_pyright_fixture(repo)
    return repo, fixture


def test_manager_reuses_matching_live_session(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "repo")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    try:
        first = manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert first is second
        assert len(manager._sessions) == 1
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_manager_keys_by_checkout_and_profile_identity(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a, fixture_a = _make_repo_fixture(tmp_path, "a")
    repo_b, fixture_b = _make_repo_fixture(tmp_path, "b")
    scope_a = resolve_repository_scope(repo_a)
    scope_b = resolve_repository_scope(repo_b)
    _patch_discovery(
        monkeypatch,
        {
            scope_a.checkout_id: fixture_a.identity,
            scope_b.checkout_id: fixture_b.identity,
        },
    )
    manager = PyrightSessionManager(state_root=state_root)
    try:
        session_a = manager.get(scope_a, deadline=time.monotonic() + SHORT_TIMEOUT)
        session_b = manager.get(scope_b, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session_a is not session_b
        assert len(manager._sessions) == 2
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_manager_key_includes_complete_qualified_identity(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyright_profile

    repo, fixture = _make_repo_fixture(tmp_path, "full-identity")
    scope = resolve_repository_scope(repo)
    first_identity = fixture.identity
    second_identity = replace(
        fixture.identity,
        executable_sha256="c" * 64,
        package_sha256="d" * 64,
    )
    discovered = iter((first_identity, second_identity))
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda *_args, **_kwargs: next(discovered),
    )
    manager = PyrightSessionManager(state_root=state_root)
    try:
        first = manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert first is not second
        assert first._identity is first_identity
        assert second._identity is second_identity
        assert len(manager._sessions) == 2
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_session_idle_close_reservation_blocks_new_operations(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )

    with session._operation():
        assert session._reserve_idle_close(time.monotonic() + 1) is False

    assert session._reserve_idle_close(time.monotonic() + 1) is True
    with pytest.raises(RuntimeError, match="closing"):
        with session._operation():
            pytest.fail("operation started after close reservation")
    assert session.active_operations == 0
    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert session._closed is True


def test_session_close_reserves_before_waiting_for_active_operation(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    active = threading.Event()
    release = threading.Event()
    close_errors: list[BaseException] = []

    def hold_operation() -> None:
        with session._operation():
            active.set()
            assert release.wait(SHORT_TIMEOUT)

    def close_session() -> None:
        try:
            session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
        except BaseException as error:
            close_errors.append(error)

    operation = threading.Thread(target=hold_operation)
    closer = threading.Thread(target=close_session)
    operation.start()
    assert active.wait(SHORT_TIMEOUT)
    closer.start()
    try:
        deadline = time.monotonic() + 1
        while not session._closing and time.monotonic() < deadline:
            time.sleep(0.001)
        assert session._closing is True
        assert closer.is_alive()
        with pytest.raises(RuntimeError, match="closing"), session._operation():
            pytest.fail("operation started after close reservation")
    finally:
        release.set()
        operation.join(SHORT_TIMEOUT)
        closer.join(SHORT_TIMEOUT)

    assert not operation.is_alive()
    assert not closer.is_alive()
    assert close_errors == []
    assert session._closed is True


def test_failed_real_session_close_keeps_reservation_until_a_retry(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    import pyright_profile

    scope = resolve_repository_scope(repository)
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda *_args, **_kwargs: semantic_pyright.identity,
    )
    manager = PyrightSessionManager(state_root=state_root)
    session = manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    process = session._process
    assert process is not None
    generation = process._coordinator.active
    assert generation is not None and generation.tree is not None
    tree = generation.tree
    terminate = lsp_process.ProcessTree.terminate
    tree_fault = True

    def fail_tree(current: object, *, deadline: float) -> None:
        if current is tree and tree_fault:
            raise OSError("retained session tree close failed")
        terminate(current, deadline=deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(lsp_process.ProcessTree, "terminate", fail_tree)
    try:
        assert session._reserve_idle_close(time.monotonic() + 1) is True
        with pytest.raises(OSError, match="retained session tree close failed"):
            session.close(deadline=time.monotonic() + SHORT_TIMEOUT)

        assert session._closing is True
        assert session._closed is False
        with pytest.raises(RuntimeError, match="closing"), session._operation():
            pytest.fail("operation started after retained close failure")

        tree_fault = False
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session._closed is True
        assert manager._sessions == {}
    finally:
        tree_fault = False
        if manager._sessions:
            manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_session_close_state_lock_wait_obeys_absolute_deadline(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    held = threading.Event()

    release_request = threading.Event()

    def hold_state_lock() -> None:
        session._lock.acquire()
        try:
            held.set()
            release_request.wait(CONTENTION_HOLD_SECONDS)
        finally:
            session._lock.release()

    holder = threading.Thread(target=hold_state_lock)
    holder.start()
    assert held.wait(SHORT_TIMEOUT)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="state lock"):
            session.close(deadline=started + 0.05)
        assert time.monotonic() - started < CONTENTION_RETURN_SECONDS
        assert session._closed is False
    finally:
        release_request.set()
        holder.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + 30)
    assert not holder.is_alive()
    assert session._closed is True


def test_manager_evicts_lru_idle_and_never_exceeds_four(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = []
    identities: dict[str, object] = {}
    scopes = []
    for index in range(5):
        repo, fixture = _make_repo_fixture(tmp_path, f"r{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
        repos.append(repo)
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    try:
        first = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        for index in range(1, 5):
            manager.get(scopes[index], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert len(manager._sessions) == 4
        assert first._closed
        fifth = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert fifth is not first
        assert len(manager._sessions) == 4
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_manager_failed_eviction_retains_slot_and_closes_outside_global_lock(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes: list[RepositoryScope] = []
    identities: dict[str, object] = {}
    for index in range(5):
        repo, fixture = _make_repo_fixture(tmp_path, f"eviction-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    retained = [
        manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        for scope in scopes[:4]
    ]
    for index, session in enumerate(retained):
        with session._lock:
            session._last_used_monotonic = float(index)
    lock_was_available = threading.Event()

    def fail_close(*, deadline: float) -> None:
        _probe_lock_from_thread(manager._lock, lock_was_available, deadline)
        raise OSError("injected close failure")

    try:
        _expect_failed_eviction_close(monkeypatch, manager, retained[0], fail_close, scopes[4])

        assert lock_was_available.is_set()
        assert len(manager._sessions) == 4
        assert retained[0] in manager._sessions.values()
        assert retained[0]._closed is False
        assert not _sessions_for_checkout(manager, scopes[4].checkout_id)
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_manager_returns_not_ready_when_all_four_active(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = []
    identities: dict[str, object] = {}
    scopes = []
    for index in range(5):
        repo, fixture = _make_repo_fixture(tmp_path, f"active{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
        repos.append(repo)
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    sessions: list[PyrightSession] = []
    try:
        for index in range(4):
            session = manager.get(scopes[index], deadline=time.monotonic() + SHORT_TIMEOUT)
            sessions.append(session)
            session._active_operations = 1
        denied = manager.get(scopes[4], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert denied._capacity_locked is True
        assert denied.start(deadline=time.monotonic() + SHORT_TIMEOUT) is None
        assert denied.readiness == "not_ready"
        assert "pyright_capacity_exhausted" in denied.degradation_codes
        assert len(manager._sessions) == 4
    finally:
        _release_active_operations(sessions)
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_manager_close_all_closes_every_retained_session(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = []
    identities: dict[str, object] = {}
    scopes = []
    for index in range(2):
        repo, fixture = _make_repo_fixture(tmp_path, f"close{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = fixture.identity
        repos.append(repo)
    _patch_discovery(monkeypatch, identities)
    manager = PyrightSessionManager(state_root=state_root)
    retained = [
        manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT),
        manager.get(scopes[1], deadline=time.monotonic() + SHORT_TIMEOUT),
    ]
    manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert all(session._closed for session in retained)
    assert manager._sessions == {}


def test_manager_close_all_retains_failures_continues_and_rethrows_first_interrupt(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = _prepared_scopes(tmp_path, monkeypatch, "interrupt", 4)
    manager = PyrightSessionManager(state_root=state_root)
    sessions = [
        manager.get(scope, deadline=time.monotonic() + 10)
        for scope in scopes
    ]
    attempts: list[int] = []
    real_close = [session.close for session in sessions]

    first_failures = {
        0: OSError("first close failed"),
        1: KeyboardInterrupt("first interruption"),
        2: SystemExit(29),
    }

    def flaky_close(index: int, *, deadline: float) -> None:
        attempts.append(index)
        if attempts.count(index) == 1 and index in first_failures:
            raise first_failures[index]
        real_close[index](deadline=deadline)

    with monkeypatch.context() as patch:
        for index, session in enumerate(sessions):
            patch.setattr(
                session,
                "close",
                lambda *, deadline, index=index: flaky_close(
                    index,
                    deadline=deadline,
                ),
            )

        with pytest.raises(KeyboardInterrupt, match="first interruption"):
            manager.close_all(deadline=time.monotonic() + 5)

        assert attempts == [0, 1, 2, 3]
        assert set(manager._sessions.values()) == set(sessions[:3])
        assert sessions[3]._closed is True

        manager.close_all(deadline=time.monotonic() + 5)

    assert attempts == [0, 1, 2, 3, 0, 1, 2]
    assert manager._sessions == {}
    assert all(session._closed for session in sessions)


def test_manager_close_all_unwraps_earliest_interruption_without_cycle(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = _prepared_scopes(tmp_path, monkeypatch, "wrapped-interrupt", 3)
    manager = PyrightSessionManager(state_root=state_root)
    sessions = [
        manager.get(scope, deadline=time.monotonic() + 10)
        for scope in scopes
    ]
    real_close = [session.close for session in sessions]
    attempts: list[int] = []
    ordinary_error = OSError("later ordinary manager close error")
    interruption = SystemExit(53)
    wrapper = RuntimeError("manager close interruption wrapper")
    wrapper.__cause__ = interruption

    first_failures = {0: wrapper, 1: ordinary_error}

    def flaky_close(index: int, *, deadline: float) -> None:
        attempts.append(index)
        if attempts.count(index) == 1 and index in first_failures:
            raise first_failures[index]
        real_close[index](deadline=deadline)

    with monkeypatch.context() as patch:
        for index, session in enumerate(sessions):
            patch.setattr(
                session,
                "close",
                lambda *, deadline, index=index: flaky_close(
                    index,
                    deadline=deadline,
                ),
            )

        with pytest.raises(SystemExit) as raised:
            manager.close_all(deadline=time.monotonic() + 5)

        assert raised.value is interruption
        assert raised.value.__cause__ is ordinary_error
        assert raised.value.__context__ is None
        assert wrapper.__cause__ is interruption
        assert attempts == [0, 1, 2]
        assert set(manager._sessions.values()) == set(sessions[:2])

        manager.close_all(deadline=time.monotonic() + 5)

    assert attempts == [0, 1, 2, 0, 1]
    assert manager._sessions == {}
    assert all(session._closed for session in sessions)


@pytest.mark.parametrize("operation", ["get", "close_all"])
def test_manager_global_lock_wait_obeys_absolute_deadline(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, f"deadline-{operation}")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    held = threading.Event()
    release = threading.Event()

    def hold_manager_lock() -> None:
        manager._lock.acquire()
        try:
            held.set()
            assert release.wait(SHORT_TIMEOUT)
        finally:
            manager._lock.release()

    holder = threading.Thread(target=hold_manager_lock)
    holder.start()
    assert held.wait(SHORT_TIMEOUT)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="manager lock"):
            _run_manager_operation(manager, scope, operation, deadline=started + 0.05)
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        holder.join(SHORT_TIMEOUT)
        manager.close_all(deadline=time.monotonic() + 2)
    assert not holder.is_alive()


def test_manager_get_rechecks_closed_after_per_key_wait(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyright_profile

    repo, fixture = _make_repo_fixture(tmp_path, "key-wait-close")
    scope = resolve_repository_scope(repo)
    discovered = threading.Event()

    def discover(*_args: object, **_kwargs: object) -> PyrightIdentity:
        discovered.set()
        return fixture.identity  # type: ignore[return-value]

    monkeypatch.setattr(pyright_profile, "discover_pyright", discover)
    manager = PyrightSessionManager(state_root=state_root)
    key = manager._profile_key(scope, fixture.identity)  # type: ignore[arg-type]
    with manager._lock:
        key_lock_state = manager._retain_key_lock_locked(
            key,
            time.monotonic() + 1,
        )
    key_lock_state.lock.acquire()
    errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    close_done = threading.Event()

    def get_waiting() -> None:
        try:
            manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=get_waiting)
    waiter.start()
    assert discovered.wait(SHORT_TIMEOUT)
    try:
        def close_manager() -> None:
            try:
                manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
            except BaseException as error:
                close_errors.append(error)
            finally:
                close_done.set()

        closer = threading.Thread(target=close_manager)
        closer.start()
        _await_manager_closed(manager, time.monotonic() + 1)
        assert close_done.wait(0.05) is False
    finally:
        key_lock_state.lock.release()
        manager._release_key_lock_reference(key, key_lock_state)
        waiter.join(SHORT_TIMEOUT)
        if closer.ident is not None:
            closer.join(SHORT_TIMEOUT)

    assert not waiter.is_alive()
    assert not closer.is_alive()
    assert close_errors == []
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "manager is closed" in str(errors[0])
    assert manager._sessions == {}


def test_manager_per_key_lock_wait_obeys_absolute_deadline(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-deadline")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    key = manager._profile_key(scope, fixture.identity)  # type: ignore[arg-type]
    with manager._lock:
        key_lock_state = manager._retain_key_lock_locked(
            key,
            time.monotonic() + 1,
        )
    key_lock_state.lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="key lock"):
            manager.get(scope, deadline=started + 0.05)
        assert time.monotonic() - started < 0.3
        assert manager._sessions == {}
    finally:
        key_lock_state.lock.release()
        manager._release_key_lock_reference(key, key_lock_state)
        manager.close_all(deadline=time.monotonic() + 2)


def test_manager_close_all_waits_for_get_retained_before_per_key_lock(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-retained-before-lock")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    reference_retained = threading.Event()
    release_get = threading.Event()
    get_done = threading.Event()
    close_done = threading.Event()
    get_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def pause_before_key_lock(lock: threading.Lock, deadline: float) -> None:
        reference_retained.set()
        if not release_get.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("retained key reference release expired")
        acquire_key_lock(lock, deadline)

    def get_session() -> None:
        try:
            manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        except BaseException as error:
            get_errors.append(error)
        finally:
            get_done.set()

    def close_manager() -> None:
        try:
            manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    monkeypatch.setattr(manager, "_acquire_key_lock", pause_before_key_lock)
    getter = threading.Thread(target=get_session, name="retained-key-getter")
    closer = threading.Thread(target=close_manager, name="retained-key-closer")
    try:
        getter.start()
        assert reference_retained.wait(SHORT_TIMEOUT)
        with manager._lock:
            assert len(manager._key_locks) == 1
            assert next(iter(manager._key_locks.values())).references == 1

        closer.start()
        assert close_done.wait(0.2) is False
        release_get.set()
        assert get_done.wait(SHORT_TIMEOUT)
        assert close_done.wait(SHORT_TIMEOUT)
    finally:
        release_get.set()
        getter.join(SHORT_TIMEOUT)
        if closer.ident is not None:
            closer.join(SHORT_TIMEOUT)
        with manager._lock:
            manager._prune_key_locks_locked()

    assert not getter.is_alive()
    assert not closer.is_alive()
    assert len(get_errors) == 1
    assert isinstance(get_errors[0], RuntimeError)
    assert "manager is closed" in str(get_errors[0])
    assert close_errors == []
    assert manager._sessions == {}
    assert manager._key_locks == {}
    assert manager._key_lock_releases.empty()


def test_manager_close_all_deadline_retains_key_reference_for_retry(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-close-race")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    key_acquired = threading.Event()
    release_get = threading.Event()
    get_done = threading.Event()
    close_done = threading.Event()
    get_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def pause_after_key_acquire(lock: threading.Lock, deadline: float) -> None:
        acquire_key_lock(lock, deadline)
        key_acquired.set()
        if not release_get.wait(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("key close-race release expired")

    def get_session() -> None:
        try:
            manager.get(scope, deadline=time.monotonic() + 5)
        except BaseException as error:
            get_errors.append(error)
        finally:
            get_done.set()

    def close_manager() -> None:
        try:
            manager.close_all(deadline=time.monotonic() + 0.1)
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_done.set()

    monkeypatch.setattr(manager, "_acquire_key_lock", pause_after_key_acquire)
    getter = threading.Thread(target=get_session)
    closer = threading.Thread(target=close_manager)
    reference_lock: threading.Lock | None = None
    try:
        getter.start()
        assert key_acquired.wait(SHORT_TIMEOUT)
        with manager._lock:
            assert len(manager._key_locks) == 1
            state = next(iter(manager._key_locks.values()))
        reference_lock = state.reference_lock
        reference_lock.acquire()

        closer.start()
        assert close_done.wait(SHORT_TIMEOUT)
        assert len(close_errors) == 1
        assert isinstance(close_errors[0], TimeoutError)
        with manager._lock:
            assert manager._key_locks
            assert state.references == 1
        release_get.set()

        assert get_done.wait(SHORT_TIMEOUT), "key reference release ignored its closed manager"
        assert len(get_errors) == 1
        assert isinstance(get_errors[0], RuntimeError)
        assert "manager is closed" in str(get_errors[0])
    finally:
        release_get.set()
        if reference_lock is not None and reference_lock.locked():
            reference_lock.release()
        getter.join(SHORT_TIMEOUT)
        if closer.ident is not None:
            closer.join(SHORT_TIMEOUT)
        manager.close_all(deadline=time.monotonic() + 2)
        with manager._lock:
            manager._prune_key_locks_locked()

    assert not getter.is_alive()
    assert not closer.is_alive()
    assert manager._key_locks == {}
    assert manager._key_lock_releases.empty()


def test_manager_reference_gate_deadline_stays_bounded_across_sequential_waiters(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-reference-deadline")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    keeper_entered = threading.Event()
    release_keeper = threading.Event()
    keeper_errors: list[BaseException] = []
    keeper_results: list[PyrightSession] = []

    def hold_keeper_before_key_acquire(
        lock: threading.Lock,
        deadline: float,
    ) -> None:
        if threading.current_thread().name == "key-reference-keeper":
            keeper_entered.set()
            if not release_keeper.wait(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("reference keeper release expired")
        acquire_key_lock(lock, deadline)

    def keep_reference() -> None:
        try:
            # The keeper only has to outlive the hundred contenders below; ten
            # seconds was shorter than that loop on a hosted macOS runner, so
            # the fixture expired instead of the subject under test.
            keeper_results.append(
                manager.get(scope, deadline=time.monotonic() + 180)
            )
        except BaseException as error:
            keeper_errors.append(error)

    monkeypatch.setattr(manager, "_acquire_key_lock", hold_keeper_before_key_acquire)
    keeper = threading.Thread(target=keep_reference, name="key-reference-keeper")
    reference_lock: threading.Lock | None = None
    try:
        keeper.start()
        assert keeper_entered.wait(SHORT_TIMEOUT)
        with manager._lock:
            assert len(manager._key_locks) == 1
            state = next(iter(manager._key_locks.values()))
        reference_lock = state.reference_lock
        reference_lock.acquire()

        for index in range(100):
            errors: list[BaseException] = []
            done = threading.Event()

            def contend() -> None:
                try:
                    manager.get(scope, deadline=time.monotonic() + 0.02)
                except BaseException as error:
                    errors.append(error)
                finally:
                    done.set()

            contender = threading.Thread(
                target=contend,
                name=f"key-reference-contender-{index}",
            )
            contender.start()
            assert done.wait(SHORT_TIMEOUT), f"reference contender {index} exceeded its deadline"
            contender.join(SHORT_TIMEOUT)
            assert not contender.is_alive()
            assert len(errors) == 1
            assert isinstance(errors[0], TimeoutError)
            assert "reference" in str(errors[0])
    finally:
        if reference_lock is not None and reference_lock.locked():
            reference_lock.release()
        release_keeper.set()
        keeper.join(SHORT_TIMEOUT)
        manager.close_all(deadline=time.monotonic() + 3)

    assert not keeper.is_alive()
    assert keeper_errors == []
    assert len(keeper_results) == 1
    assert manager._key_locks == {}


def test_manager_key_lock_lives_through_waiters_and_releases_after_last_get(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "key-lifecycle")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    acquire_key_lock = manager._acquire_key_lock
    calls_lock = threading.Lock()
    acquire_calls = 0
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_waiting = threading.Event()
    second_acquired = threading.Event()
    release_second = threading.Event()
    third_waiting = threading.Event()
    third_done = threading.Event()
    results: list[PyrightSession] = []
    errors: list[BaseException] = []

    # By call number: the event set before waiting for the lock, and the pair
    # (set once held, wait for release) after acquiring it.
    waiting_by_call = {2: second_waiting, 3: third_waiting}
    held_by_call = {1: (first_acquired, release_first), 2: (second_acquired, release_second)}

    def controlled_acquire(lock: threading.Lock, deadline: float) -> None:
        nonlocal acquire_calls
        with calls_lock:
            acquire_calls += 1
            call = acquire_calls
        if call in waiting_by_call:
            waiting_by_call[call].set()
        acquire_key_lock(lock, deadline)
        if call in held_by_call:
            acquired, release = held_by_call[call]
            acquired.set()
            assert release.wait(SHORT_TIMEOUT)

    def get_session(*, third: bool = False) -> None:
        try:
            results.append(manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT))
        except BaseException as error:
            errors.append(error)
        finally:
            if third:
                third_done.set()

    monkeypatch.setattr(manager, "_acquire_key_lock", controlled_acquire)
    first = threading.Thread(target=get_session)
    second = threading.Thread(target=get_session)
    third = threading.Thread(target=get_session, kwargs={"third": True})
    try:
        first.start()
        assert first_acquired.wait(SHORT_TIMEOUT)
        second.start()
        assert second_waiting.wait(SHORT_TIMEOUT)
        release_first.set()
        first.join(SHORT_TIMEOUT)
        assert not first.is_alive()
        assert second_acquired.wait(SHORT_TIMEOUT)

        third.start()
        assert third_waiting.wait(SHORT_TIMEOUT)
        assert third_done.wait(0.05) is False
        release_second.set()
        second.join(SHORT_TIMEOUT)
        third.join(SHORT_TIMEOUT)

        assert not second.is_alive()
        assert not third.is_alive()
        assert errors == []
        assert len(results) == 3
        assert results[0] is results[1] is results[2]
        assert manager._key_locks == {}
    finally:
        release_first.set()
        release_second.set()
        _join_started_threads((first, second, third))
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert manager._key_locks == {}


def test_manager_key_locks_do_not_accumulate_across_profile_evictions(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyright_profile

    repo, fixture = _make_repo_fixture(tmp_path, "key-churn")
    scope = resolve_repository_scope(repo)
    identities = iter(
        replace(fixture.identity, executable_sha256=f"{index:064x}")
        for index in range(12)
    )
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda *_args, **_kwargs: next(identities),
    )
    manager = PyrightSessionManager(state_root=state_root)
    try:
        for _index in range(12):
            manager.get(scope, deadline=time.monotonic() + 5)
            assert manager._key_locks == {}
            assert len(manager._sessions) <= MAX_LSP_PROCESSES
    finally:
        manager.close_all(deadline=time.monotonic() + 2)

    assert manager._key_locks == {}


def test_manager_parallel_evictions_reserve_distinct_idle_sessions_with_capacity_four(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = _prepared_scopes(tmp_path, monkeypatch, "parallel", 6)
    manager = PyrightSessionManager(state_root=state_root)
    retained = [
        manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        for scope in scopes[:4]
    ]
    _stagger_last_used(retained)
    close_entered = [threading.Event(), threading.Event()]
    release_close = threading.Event()
    real_close = [retained[index].close for index in range(2)]

    def blocked_close(index: int, *, deadline: float) -> None:
        close_entered[index].set()
        _wait_for_release(release_close, deadline)
        real_close[index](deadline=deadline)

    results: list[PyrightSession] = []
    errors: list[BaseException] = []

    def get_new(scope: RepositoryScope) -> None:
        try:
            results.append(manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT))
        except BaseException as error:
            errors.append(error)

    try:
        with monkeypatch.context() as patch:
            _patch_blocked_closes(patch, retained[:2], blocked_close)
            getters = _started_threads(get_new, scopes[4:])
            assert all(event.wait(SHORT_TIMEOUT) for event in close_entered)
            _assert_evictions_reserved(manager, retained[:2])
            release_close.set()
            _join_all(getters)

        assert errors == []
        assert len(results) == 2
        assert len(manager._sessions) == MAX_LSP_PROCESSES
        assert _all_retained(manager, results)
        assert retained[0]._closed is True
        assert retained[1]._closed is True
    finally:
        release_close.set()
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_manager_registers_atexit_once(
    tmp_path: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, fixture = _make_repo_fixture(tmp_path, "atexit")
    scope = resolve_repository_scope(repo)
    _patch_discovery(monkeypatch, {scope.checkout_id: fixture.identity})
    manager = PyrightSessionManager(state_root=state_root)
    assert manager._atexit_registered is False
    manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert manager._atexit_registered is True
    manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert manager._atexit_registered is True
    manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
