"""The MCP server owns one navigation manager: its lock honours deadlines, it refuses cancelled and stale requests, and close resets it only after success."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from llm_wiki import mcp_server_navigation

from tests.mcp_server_support import (
    _assert_failed_close_retains_owner,
    _assert_second_close_clears_owner,
    _replace_in_every_server_part,
)


def test_navigation_manager_singleton_lock_honors_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server

    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", None)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 7, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", lock)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="manager lock"):
        mcp_server.navigation_session_manager(
            started + 0.02,
            7,
            lambda: False,
        )

    assert time.monotonic() - started < 0.2
    lock.release()


def test_navigation_manager_existing_lookup_still_honors_lock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server

    lock = threading.Lock()
    lock.acquire()
    manager = object()
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", manager)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 7, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", lock)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="manager lock"):
        mcp_server.navigation_session_manager(
            started + 0.02,
            7,
            lambda: False,
        )

    assert time.monotonic() - started < 0.2
    lock.release()


def test_close_navigation_manager_resets_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    closed: list[float] = []

    class Manager:
        def close_all(self, *, deadline: float) -> None:
            closed.append(deadline)

    manager = Manager()
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", manager)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 11, raising=False)
    deadline = time.monotonic() + 5

    mcp_server.close_navigation_session_manager(deadline)

    assert closed == [deadline]
    assert mcp_server_navigation.NAVIGATION_MANAGER is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_CLOSING is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_EPOCH == 12


def test_close_navigation_manager_advances_epoch_without_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", None)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 23, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", threading.Lock())

    mcp_server.close_navigation_session_manager(time.monotonic() + 5)

    assert mcp_server_navigation.NAVIGATION_MANAGER_EPOCH == 24
    assert mcp_server_navigation.NAVIGATION_MANAGER is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_CLOSING is None


def test_navigation_manager_rejects_cancelled_and_stale_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server
    from llm_wiki import pyright_session_manager

    def forbidden(*_args, **_kwargs):
        raise AssertionError("rejected request constructed a manager")

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", None)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 5, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", threading.Lock())
    monkeypatch.setattr(pyright_session_manager, "PyrightSessionManager", forbidden)
    deadline = time.monotonic() + 5

    with pytest.raises(TimeoutError, match="cancelled"):
        mcp_server.navigation_session_manager(deadline, 5, lambda: True)
    with pytest.raises(TimeoutError, match="lifecycle"):
        mcp_server.navigation_session_manager(deadline, 4, lambda: False)

    assert mcp_server_navigation.NAVIGATION_MANAGER is None


def test_close_navigation_manager_retains_failed_owner_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    attempts = 0

    class Manager:
        def close_all(self, *, deadline: float) -> None:
            nonlocal attempts
            del deadline
            attempts += 1
            if attempts == 1:
                raise TimeoutError("close failed")

    manager = Manager()
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", manager)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 31, raising=False)

    with pytest.raises(TimeoutError, match="close failed"):
        mcp_server.close_navigation_session_manager(time.monotonic() + 5)

    _assert_failed_close_retains_owner(manager)
    with pytest.raises(TimeoutError, match="closing"):
        mcp_server.navigation_session_manager(
            time.monotonic() + 0.02,
            mcp_server_navigation.NAVIGATION_MANAGER_EPOCH,
            lambda: False,
        )

    mcp_server.close_navigation_session_manager(time.monotonic() + 5)

    _assert_second_close_clears_owner(attempts)


def test_navigation_manager_constructor_crossing_deadline_is_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server
    from llm_wiki import pyright_session_manager

    now = [100.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return now[0]

    class Manager:
        def __init__(self, *, state_root: Path) -> None:
            del state_root
            now[0] = 101.0

    _replace_in_every_server_part(monkeypatch, "time", Clock)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", None)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 2, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", threading.Lock())
    monkeypatch.setattr(pyright_session_manager, "PyrightSessionManager", Manager)

    with pytest.raises(TimeoutError):
        mcp_server.navigation_session_manager(100.5, 2, lambda: False)

    assert mcp_server_navigation.NAVIGATION_MANAGER is None


def test_successful_close_crossing_deadline_still_resets_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import mcp_server

    now = [100.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return now[0]

    class Manager:
        def close_all(self, *, deadline: float) -> None:
            assert deadline == 100.5
            now[0] = 101.0

    manager = Manager()
    _replace_in_every_server_part(monkeypatch, "time", Clock)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", manager)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 41, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", threading.Lock())

    with pytest.raises(TimeoutError):
        mcp_server.close_navigation_session_manager(100.5)

    assert mcp_server_navigation.NAVIGATION_MANAGER is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_CLOSING is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_EPOCH == 42
