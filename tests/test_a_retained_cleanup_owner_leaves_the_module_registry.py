"""A session that takes over a startup cleanup takes it out of the registry.

Finding K-C7 of the 2026-09-17 audit, the reverse case: `transfer_cleanup_ownership`
has a production caller (`pyright_session._retain_startup_cleanup_locked`) and no
test of its own. The module registry is bounded at eight owners, so an owner a
session has taken over and does not give back would spend one of those slots for
the life of the process.
"""

from __future__ import annotations

import time
from pathlib import Path

import lsp_process
from lsp_process import StartupCleanupError
from pyright_session import PyrightSession
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_python_repository
from tests.slow_machine import SHORT_TIMEOUT


def _registered() -> set[int]:
    return {
        id(coordinator)
        for coordinator in lsp_process._pending_startup_cleanup_snapshot()
    }


def _registered_coordinator() -> lsp_process._LifecycleCoordinator:
    coordinator = lsp_process._LifecycleCoordinator(None)
    lsp_process._register_startup_cleanup(coordinator)
    return coordinator


def test_transfer_removes_the_owner_from_the_module_registry() -> None:
    coordinator = _registered_coordinator()
    error = StartupCleanupError("retained owner", coordinator=coordinator)
    before = _registered()
    try:
        error.transfer_cleanup_ownership()
        assert (id(coordinator) in before, id(coordinator) in _registered()) == (
            True,
            False,
        )
    finally:
        lsp_process._unregister_startup_cleanup(coordinator)


def test_transfer_without_an_owner_changes_nothing() -> None:
    before = _registered()

    StartupCleanupError("no owner to transfer").transfer_cleanup_ownership()

    assert _registered() == before


def test_a_session_that_retains_an_owner_transfers_it(
    tmp_path: Path, state_root: Path
) -> None:
    from pyright_profile import PyrightIdentity

    coordinator = _registered_coordinator()
    error = StartupCleanupError("session retains this owner", coordinator=coordinator)
    session = PyrightSession(
        resolve_repository_scope(create_python_repository(tmp_path / "repo")),
        PyrightIdentity(
            status="missing",
            source=None,
            version=None,
            node_executable=None,
            node_version=None,
            node_major=None,
            server_executable=None,
            executable_sha256=None,
            package_sha256=None,
            initialization_options_sha256="a" * 64,
            configuration_sha256="b" * 64,
            qualified=False,
            degradation_codes=(),
        ),
        state_root=state_root,
    )
    try:
        with session._lock:
            session._retain_startup_cleanup_locked(error)
        assert (
            session._startup_cleanup_error is error,
            id(coordinator) in _registered(),
        ) == (True, False)
    finally:
        lsp_process._unregister_startup_cleanup(coordinator)
        session._startup_cleanup_error = None
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
