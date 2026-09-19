"""A session whose close failed is closed again by the next caller that meets it.

Finding K-A11 of the 2026-09-17 audit: the failed close left `_closing` set, the
manager skipped the session for eviction and only polled the flag for the same
key, so the checkout was dead and a slot was lost until the MCP process exited.
Research: docs/research/2026-09-17-sess-a-session-that-failed-to-close-or-start-is-tried-again.md
"""

from __future__ import annotations

import time
from pathlib import Path

import lsp_process
import pyright_profile
import pytest
from pyright_session import MAX_LSP_PROCESSES, PyrightSessionManager
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import (
    create_python_repository,
    create_semantic_pyright_fixture,
)
from tests.slow_machine import SHORT_TIMEOUT


class _TreeFault:
    """Make one session's process tree refuse to terminate, until cleared."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, session) -> None:
        self.active = True
        tree = session._process._coordinator.active.tree
        original = lsp_process.ProcessTree.terminate
        fault = self

        def terminate(current: object, *, deadline: float) -> None:
            if current is tree and fault.active:
                raise OSError("injected tree close failure")
            original(current, deadline=deadline)

        monkeypatch.setattr(lsp_process.ProcessTree, "terminate", terminate)


def _manager_with_sessions(tmp_path: Path, state_root: Path, monkeypatch, count: int):
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


def _strand(manager, scope, monkeypatch) -> tuple[object, _TreeFault]:
    """A started session whose close has failed once."""
    session = manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    fault = _TreeFault(monkeypatch, session)
    with pytest.raises(OSError, match="injected tree close failure"):
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
    return session, fault


def test_the_next_get_for_the_key_finishes_the_failed_close(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, scopes = _manager_with_sessions(tmp_path, state_root, monkeypatch, 1)
    try:
        stranded, fault = _strand(manager, scopes[0], monkeypatch)
        before = (stranded._closing, stranded._closed)
        fault.active = False
        fresh = manager.get(scopes[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (before, stranded._closed, fresh is stranded, fresh._closing) == (
            (True, False),
            True,
            False,
            False,
        )
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_at_capacity_a_stranded_session_gives_up_its_slot_first(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, scopes = _manager_with_sessions(
        tmp_path, state_root, monkeypatch, MAX_LSP_PROCESSES + 1
    )
    try:
        stranded, fault = _strand(manager, scopes[0], monkeypatch)
        healthy = [
            manager.get(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
            for scope in scopes[1:MAX_LSP_PROCESSES]
        ]
        fault.active = False
        newcomer = manager.get(
            scopes[MAX_LSP_PROCESSES], deadline=time.monotonic() + SHORT_TIMEOUT
        )
        assert (
            stranded._closed,
            [session._closed for session in healthy],
            newcomer._capacity_locked,
            len(manager._sessions),
        ) == (True, [False] * (MAX_LSP_PROCESSES - 1), False, MAX_LSP_PROCESSES)
    finally:
        manager.close_all(deadline=time.monotonic() + SHORT_TIMEOUT)
