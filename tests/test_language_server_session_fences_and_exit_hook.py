from __future__ import annotations

import time
from pathlib import Path

import pytest
from llm_wiki.pyright_session_core import LanguageServerSession
from lsp_profiles import PYRIGHT_PROFILE, TYPESCRIPT_PROFILE
from repository_scope import resolve_repository_scope

from tests.pyright_session_helpers import _missing_identity


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return LanguageServerSession(
        resolve_repository_scope(repository), _missing_identity(), state_root=state_root
    )


def _refuse_close(*, deadline: float) -> None:
    raise RuntimeError(f"close refused before {deadline}")


def test_a_session_names_the_profile_it_drives(repository: Path, state_root: Path) -> None:
    session = LanguageServerSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
        profile=TYPESCRIPT_PROFILE,
    )

    assert session.profile is TYPESCRIPT_PROFILE


def test_the_exit_hook_closes_the_session(session: LanguageServerSession) -> None:
    session._atexit_cleanup()

    assert session._closed is True


def test_the_exit_hook_never_raises_when_close_fails(
    session: LanguageServerSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "close", _refuse_close)

    session._atexit_cleanup()

    assert session._closed is False


def test_a_second_synchronization_fence_is_refused_while_one_is_active(
    session: LanguageServerSession,
) -> None:
    with (
        session._synchronize_semantic_fence(),
        pytest.raises(RuntimeError, match="fence is already active"),
    ):
        session._synchronize_semantic_fence().__enter__()

    assert session._synchronize_epoch == 2


def test_a_fence_whose_epoch_moved_underneath_it_refuses_to_release(
    session: LanguageServerSession,
) -> None:
    fence = session._synchronize_semantic_fence()
    fence.__enter__()
    session._synchronize_epoch += 2

    with pytest.raises(RuntimeError, match="fence changed unexpectedly"):
        fence.__exit__(None, None, None)


@pytest.mark.parametrize("busy_flag", ["_closing", "_closed", "_starting"])
def test_a_busy_session_is_not_reserved_for_an_idle_close(
    session: LanguageServerSession, busy_flag: str
) -> None:
    setattr(session, busy_flag, True)

    assert session._reserve_idle_close(time.monotonic() + 5.0) is False


def test_an_idle_session_is_reserved_for_close(session: LanguageServerSession) -> None:
    assert session._reserve_idle_close(time.monotonic() + 5.0) is True
    assert session._closing is True


def test_a_profile_without_an_identity_notification_records_nothing(
    session: LanguageServerSession,
) -> None:
    assert PYRIGHT_PROFILE.identity_notification is None

    session._record_server_identity({"version": "1.0"})

    assert (session.progress_events, session.degradation_codes) == ((), ("pyright_missing",))
