"""A server nobody has used for five minutes is closed by the next request.

Finding K-C6 of the 2026-09-17 audit: `LspProcess.idle_expired` and the 300 s
limit existed with no production caller, so up to four servers lived until
capacity eviction or process exit.
Research: docs/research/2026-09-17-sess-a-session-that-failed-to-close-or-start-is-tried-again.md
"""

from __future__ import annotations

import time
from pathlib import Path

import lsp_process
import pytest
from pyright_session import PyrightSessionManager
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import (
    create_python_repository,
    create_semantic_pyright_fixture,
)
from tests.slow_machine import SHORT_TIMEOUT


def _manager_with_scopes(
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


def _age_process(session, seconds: float) -> None:
    """Make the server this session owns look unused for that long."""
    session._process.last_used_monotonic = time.monotonic() - seconds


def test_the_next_request_closes_a_server_idle_past_the_limit(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, scopes = _manager_with_scopes(tmp_path, state_root, monkeypatch, 2)
    try:
        idle = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        idle.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        _age_process(idle, lsp_process._IDLE_SECONDS + 1.0)
        manager.get(scopes[1], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (idle._closed, len(manager._sessions)) == (True, 1)
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_server_inside_the_limit_is_left_alone(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, scopes = _manager_with_scopes(tmp_path, state_root, monkeypatch, 2)
    try:
        recent = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        recent.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        _age_process(recent, lsp_process._IDLE_SECONDS - 1.0)
        manager.get(scopes[1], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (recent._closed, len(manager._sessions)) == (False, 2)
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_the_session_being_asked_for_is_never_reaped(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, scopes = _manager_with_scopes(tmp_path, state_root, monkeypatch, 1)
    try:
        session = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        _age_process(session, lsp_process._IDLE_SECONDS + 1.0)
        again = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (again is session, session._closed) == (True, False)
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
