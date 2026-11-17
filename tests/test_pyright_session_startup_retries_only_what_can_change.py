from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import cast

import pytest
from llm_wiki.lsp_process_owner import OwnerAclDeadlineExpired
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_startup import (
    STARTUP_RETRY_BACKOFF,
    _cleanup_interruption,
    _startup_code,
    _StartupAttempt,
)
from lsp_process import LspProcess, ProcessState, StartupCleanupError
from lsp_profiles import GOPLS_PROFILE, TYPESCRIPT_PROFILE
from lsp_server_profile import LanguageServerProfile
from repository_scope import resolve_repository_scope

from tests.language_server_session_state import unstarted_session
from tests.pyright_session_helpers import _missing_identity
from tests.slow_machine import SHORT_TIMEOUT


class _RefusingOwner:
    def __init__(self) -> None:
        self.attempts = 0

    def retry_cleanup(self, deadline: float) -> None:
        self.attempts += 1
        raise RuntimeError("cleanup refused again")

    def close(self, deadline: float) -> None:
        self.attempts += 1
        raise OSError("close refused again")


class _FailedProcess:
    state = ProcessState.FAILED


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


def _deadline() -> float:
    return time.monotonic() + SHORT_TIMEOUT


def _profiled(
    repository: Path, state_root: Path, profile: LanguageServerProfile
) -> LanguageServerSession:
    return LanguageServerSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
        profile=profile,
    )


def _refuse_with_runtime_error() -> None:
    raise RuntimeError("expected refusal")


def _refuse_with_key_error() -> None:
    raise KeyError("unexpected refusal")


def test_a_permission_refusal_caused_by_a_process_timeout_is_a_startup_timeout() -> None:
    error = PermissionError("refused")
    error.__cause__ = subprocess.TimeoutExpired("server", 1.0)

    assert _startup_code(error) == "pyright_startup_timeout"


def test_the_owner_acl_running_out_of_time_is_a_startup_timeout() -> None:
    refused = OwnerAclDeadlineExpired("owner-only LSP ACL deadline expired")

    assert _startup_code(refused) == "pyright_startup_timeout"


def test_a_permission_refusal_is_not_read_as_a_timeout_from_its_words() -> None:
    assert _startup_code(PermissionError("lease deadline expired")) == "pyright_startup_failed"


def test_no_cleanup_error_carries_no_interruption() -> None:
    assert _cleanup_interruption(None) is None


def test_waiting_on_another_startup_ends_at_the_deadline(
    session: LanguageServerSession,
) -> None:
    with session._lock:
        session._starting = True
        with pytest.raises(TimeoutError, match="startup did not finish before deadline"):
            session._await_startup_locked(time.monotonic() - 1.0)


def test_a_closed_session_admits_no_startup(session: LanguageServerSession) -> None:
    session._closed = True

    with pytest.raises(RuntimeError, match="session is closed"):
        session._admit_startup(_deadline(), 5.0)


def test_an_expected_retry_failure_is_answered_as_not_cleared(
    session: LanguageServerSession,
) -> None:
    assert session._startup_retry_ok(_refuse_with_runtime_error) is False


def test_an_unexpected_retry_failure_is_raised(session: LanguageServerSession) -> None:
    with pytest.raises(KeyError, match="unexpected refusal"):
        session._startup_retry_ok(_refuse_with_key_error)


def test_a_retained_cleanup_that_refuses_again_blocks_the_launch(
    session: LanguageServerSession,
) -> None:
    owner = _RefusingOwner()

    ready = session._ready_to_launch(cast(StartupCleanupError, owner), None, _deadline())

    assert (ready, owner.attempts) == (False, 1)


def test_a_retained_process_that_refuses_to_close_again_blocks_the_launch(
    session: LanguageServerSession,
) -> None:
    owner = _RefusingOwner()

    cleared = session._clear_retained_owners(None, cast(LspProcess, owner), _deadline())

    assert (cleared, owner.attempts) == (False, 1)


def test_a_native_profile_launches_its_native_copy(repository: Path, state_root: Path) -> None:
    native = _profiled(repository, state_root, GOPLS_PROFILE)

    assert native._launch_strategy_arguments() == {"native": True}


def test_a_packaged_profile_launches_through_its_package(
    repository: Path, state_root: Path
) -> None:
    packaged = _profiled(repository, state_root, TYPESCRIPT_PROFILE)

    assert packaged._launch_strategy_arguments() == {
        "package_launch": TYPESCRIPT_PROFILE.package_launch
    }


def test_a_malformed_launch_request_is_raised_and_leaves_the_start_retryable(
    session: LanguageServerSession,
) -> None:
    session._startup_attempted = True

    with pytest.raises(ValueError, match="malformed"):
        session._require_degrading_launch_error(ValueError("malformed"))

    assert session._startup_attempted is False


def test_a_launch_error_that_is_not_a_degradation_is_raised(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(KeyError, match="foreign"):
        session._require_degrading_launch_error(KeyError("foreign"))


def test_an_abandoned_startup_without_a_process_raises_what_stopped_it(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(RuntimeError, match="handshake failed"):
        session._abandon_startup(
            RuntimeError("handshake failed"),
            None,
            _StartupAttempt(),
            startup_deadline=_deadline(),
        )


def test_a_failed_process_past_its_retry_budget_stays_with_the_session(
    session: LanguageServerSession,
) -> None:
    failed = cast(LspProcess, _FailedProcess())
    session._process = failed
    session._process_started_at = time.monotonic()
    session._startup_retries = len(STARTUP_RETRY_BACKOFF)

    with session._lock:
        session._retire_failed_process_locked()

    assert (session._process, session._startup_retries) == (failed, len(STARTUP_RETRY_BACKOFF))
