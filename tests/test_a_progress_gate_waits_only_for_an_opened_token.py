"""A progress gate waits the whole deadline only for a server that opened a token.

Split from the transport tests of finding K-B17 of the 2026-09-17 audit.
Research: docs/research/2026-09-18-sess-a-lapsed-deadline-is-not-a-torn-stream.md
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from llm_wiki import pyright_session_progress
from llm_wiki.pyright_session_core import PyrightSession
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import create_python_repository
from tests.slow_machine import SHORT_TIMEOUT


def _gated_session(tmp_path: Path, state_root: Path) -> PyrightSession:
    from lsp_profiles import TYPESCRIPT_PROFILE
    from pyright_profile import PyrightIdentity

    repository = create_python_repository(tmp_path / "repo")
    return PyrightSession(
        resolve_repository_scope(repository),
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
        profile=TYPESCRIPT_PROFILE,
    )


def test_a_server_that_opens_no_token_does_not_cost_the_whole_deadline(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pyright_session_progress, "PROGRESS_GATE_GRACE_SECONDS", 0.05
    )
    session = _gated_session(tmp_path, state_root)
    started = time.monotonic()
    try:
        session._await_progress_gate(started + SHORT_TIMEOUT)
        assert time.monotonic() - started < SHORT_TIMEOUT / 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_server_that_opened_a_token_still_gets_the_whole_deadline(
    tmp_path: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pyright_session_progress, "PROGRESS_GATE_GRACE_SECONDS", 0.01
    )
    session = _gated_session(tmp_path, state_root)
    session._retain_work_done_token("loading")
    started = time.monotonic()
    try:
        session._await_progress_gate(started + 0.2)
        assert time.monotonic() - started >= 0.2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
