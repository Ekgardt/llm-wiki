"""A start that ran out of time is tried again; a broken install is not.

Finding K-B13 of the 2026-09-17 audit: one cold query with a short deadline
left `pyright_startup_timeout` on the session for the life of the MCP process,
because `_startup_attempted` was never cleared, and the spent session kept its
capacity slot ahead of healthy neighbours.
Research: docs/research/2026-09-17-sess-a-session-that-failed-to-close-or-start-is-tried-again.md
"""

from __future__ import annotations

import time
from pathlib import Path

import pyright_session as pyright_session_module
import pytest
from pyright_session import MAX_LSP_PROCESSES, PyrightSession, PyrightSessionManager
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import (
    create_python_repository,
    create_semantic_pyright_fixture,
)
from tests.slow_machine import SHORT_TIMEOUT


class _InitializeFault:
    """Make `initialize` run out of time for the first `failures` attempts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, failures: int) -> None:
        self.attempts = 0
        request = pyright_session_module.LspProtocol.request
        fault = self

        def timed_out(
            protocol: object,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            if method == "initialize":
                fault.attempts += 1
                if fault.attempts <= failures:
                    raise TimeoutError("injected initialize deadline expired")
            return request(
                protocol, method, params, deadline=deadline, cancellation=cancellation
            )

        monkeypatch.setattr(
            pyright_session_module.LspProtocol, "request", timed_out
        )


def _started_session(repository: Path, state_root: Path) -> PyrightSession:
    fixture = create_semantic_pyright_fixture(repository)
    return PyrightSession(
        resolve_repository_scope(repository),
        fixture.identity,
        state_root=state_root,
    )


def _retry_now(session: PyrightSession) -> None:
    """Let the backoff the last failure scheduled have passed."""
    with session._lock:
        session._startup_retry_after = time.monotonic()


def test_a_timed_out_start_waits_for_its_backoff_and_then_succeeds(
    repository: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _started_session(repository, state_root)
    fault = _InitializeFault(monkeypatch, 1)
    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        spent = (session.readiness, session.degradation_codes, fault.attempts)
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        refused = (session.degradation_codes, fault.attempts)
        _retry_now(session)
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (spent, refused, session.readiness, session.degradation_codes) == (
            ("not_ready", ("pyright_startup_timeout",), 1),
            (("pyright_startup_timeout",), 1),
            "protocol_initialized",
            (),
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_the_number_of_retries_is_bounded(
    repository: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = len(pyright_session_module._STARTUP_RETRY_BACKOFF) + 1
    session = _started_session(repository, state_root)
    _InitializeFault(monkeypatch, attempts + 1)
    try:
        for _ in range(attempts + 1):
            session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
            _retry_now(session)
        assert (session._startup_retries, session._startup_attempted) == (
            attempts - 1,
            True,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_broken_install_is_never_tried_again(
    repository: Path, state_root: Path
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository, config={"initialize_behavior": "broken"}
    )
    session = PyrightSession(
        resolve_repository_scope(repository),
        fixture.identity,
        state_root=state_root,
    )
    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        _retry_now(session)
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (
            session.degradation_codes,
            session._startup_retries,
            session._startup_attempted,
        ) == (("pyright_startup_failed",), 0, True)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def _manager_with_sessions(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch, count: int
):
    import pyright_profile

    scopes = []
    identities = {}
    for index in range(count):
        repo = create_python_repository(tmp_path / f"repo-{index}")
        scope = resolve_repository_scope(repo)
        scopes.append(scope)
        identities[scope.checkout_id] = create_semantic_pyright_fixture(repo).identity
    monkeypatch.setattr(
        pyright_profile,
        "discover_pyright",
        lambda repository, **_kwargs: identities[repository.checkout_id],
    )
    return PyrightSessionManager(state_root=state_root), scopes


def _start_and_age(sessions: list[PyrightSession]) -> None:
    """Give each session a live server and the oldest possible last-used time."""
    for session in sessions:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        with session._lock:
            session._last_used_monotonic = 0.0


def test_a_session_that_owns_no_server_gives_up_its_slot_first(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, scopes = _manager_with_sessions(
        tmp_path, state_root, monkeypatch, MAX_LSP_PROCESSES + 1
    )
    try:
        sessions = [
            manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
            for scope in scopes[:MAX_LSP_PROCESSES]
        ]
        _start_and_age(sessions[1:])
        manager.get(
            scopes[MAX_LSP_PROCESSES], deadline=time.monotonic() + SHORT_TIMEOUT
        )
        assert [session._closed for session in sessions] == [True] + [False] * (
            MAX_LSP_PROCESSES - 1
        )
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
