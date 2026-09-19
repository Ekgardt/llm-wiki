"""The engine a server says it loaded reaches the session, and is judged.

Finding K-B16 of the 2026-09-17 audit: `lsp_protocol.SERVER_NOTIFICATIONS` was
Pyright's own set, so `$/typescriptVersion` was dropped by the transport and
the profile's post-initialize identity assertion was registered but never
reached; and what it recorded, nothing read.
Research: docs/research/2026-09-18-sess-the-transport-carries-what-the-profiles-declare.md
"""

from __future__ import annotations

import time
from pathlib import Path

import lsp_profiles
import lsp_protocol
from lsp_profiles import TYPESCRIPT_PROFILE
from pyright_profile import PyrightIdentity
from pyright_session import PyrightSession
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_python_repository
from tests.slow_machine import SHORT_TIMEOUT

_IDENTITY = TYPESCRIPT_PROFILE.identity_notification


def _unqualified_identity() -> PyrightIdentity:
    return PyrightIdentity(
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
    )


def _typescript_session(tmp_path: Path, state_root: Path) -> PyrightSession:
    repository = create_python_repository(tmp_path / "repo")
    return PyrightSession(
        resolve_repository_scope(repository),
        _unqualified_identity(),
        state_root=state_root,
        profile=TYPESCRIPT_PROFILE,
    )


def _announce(session: PyrightSession, source: str) -> tuple[str, ...]:
    """Deliver one `$/typescriptVersion` the way the transport would."""
    session._server_notification_handlers()[_IDENTITY.method](
        {"version": "5.9.3", "source": source}
    )
    return session.degradation_codes


def test_the_transport_carries_the_identity_method() -> None:
    assert (
        lsp_protocol.SERVER_NOTIFICATIONS == lsp_profiles.server_notification_union(),
        _IDENTITY.method in lsp_protocol.SERVER_NOTIFICATIONS,
    ) == (True, True)


def test_the_session_registers_a_handler_for_it(
    tmp_path: Path, state_root: Path
) -> None:
    session = _typescript_session(tmp_path, state_root)
    try:
        assert _IDENTITY.method in session._server_notification_handlers()
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_the_pinned_engine_leaves_the_session_undegraded(
    tmp_path: Path, state_root: Path
) -> None:
    session = _typescript_session(tmp_path, state_root)
    try:
        codes = _announce(session, _IDENTITY.required_source)
        assert (codes, session.progress_events[-1]) == (
            (),
            (_IDENTITY.method, "5.9.3", "True"),
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_another_engine_is_reported_as_a_degradation(
    tmp_path: Path, state_root: Path
) -> None:
    session = _typescript_session(tmp_path, state_root)
    try:
        codes = _announce(session, "bundled")
        assert (codes, session.progress_events[-1]) == (
            ("typescript_server_identity_unconfirmed",),
            (_IDENTITY.method, "5.9.3", "False"),
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
