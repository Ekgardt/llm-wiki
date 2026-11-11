"""After the final close a timed-out worker cannot recreate the navigation manager, and the server closes the manager when it exits."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from llm_wiki import mcp_server_navigation
from lsp_profiles import PYRIGHT_PROFILE

from tests.mcp_server_support import (
    _assert_fresh_manager_after_close,
    _assert_stale_worker_was_abandoned,
)
from tests.slow_machine import SHORT_TIMEOUT


def test_timed_out_worker_cannot_recreate_manager_after_final_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import code_navigation
    import code_navigation_renderer
    import lsp_security
    import mcp_server
    import repository_scope
    from llm_wiki import pyright_session_manager

    source = tmp_path / "api.py"
    source.write_text("def api():\n    return 1\n", encoding="utf-8")
    scope = repository_scope.resolve_repository_scope(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    stale_results = []
    constructed = []

    class Session:
        identity = object()
        profile = PYRIGHT_PROFILE
        degradation_codes = ()

    class Manager:
        def __init__(self, *, state_root: Path) -> None:
            del state_root
            constructed.append(self)

        def get(self, _scope, *, deadline: float, profile=PYRIGHT_PROFILE):
            assert deadline > time.monotonic()
            assert profile is PYRIGHT_PROFILE
            return Session()

        def close_all(self, *, deadline: float) -> None:
            assert deadline > time.monotonic()

    class Navigation:
        def __init__(self, *_args, **_kwargs):
            pass

        def query(self, *_args, **_kwargs):
            return object()

    def delayed_directory(_directory, *, deadline=None):
        del deadline
        if not entered.is_set():
            entered.set()
            assert release.wait(SHORT_TIMEOUT)
        return tmp_path, None

    def request():
        try:
            stale_results.append(
                mcp_server.get_precise_architecture(
                    str(tmp_path),
                    mode="definition",
                    path="api.py",
                    line=1,
                    character=4,
                    deadline=time.monotonic() + 5,
                )
            )
        finally:
            completed.set()

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER", None)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_EPOCH", 101, raising=False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.NAVIGATION_MANAGER_LOCK", threading.Lock())
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", set())
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())
    monkeypatch.setattr("llm_wiki.mcp_server_validation.validated_code_directory", delayed_directory)
    monkeypatch.setattr("llm_wiki.mcp_server_workers.operation_cancelled", lambda: lambda: False)
    monkeypatch.setattr("llm_wiki.mcp_server_navigation.same_filesystem_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        repository_scope,
        "resolve_repository_scope",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(
        lsp_security,
        "resolve_repository_source",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(pyright_session_manager, "PyrightSessionManager", Manager)
    monkeypatch.setattr(code_navigation, "CodeNavigation", Navigation)
    monkeypatch.setattr(
        code_navigation_renderer,
        "render_navigation",
        lambda _result: {"status": "ok", "warnings": (), "provenance": []},
    )

    async def time_out_request():
        with pytest.raises(TimeoutError, match="deadline"):
            await mcp_server.run_bounded(
                request,
                deadline=time.monotonic() + 0.05,
            )

    try:
        asyncio.run(time_out_request())
        assert entered.is_set()
        mcp_server.close_navigation_session_manager(time.monotonic() + 5)
        assert mcp_server_navigation.NAVIGATION_MANAGER_EPOCH == 102
        release.set()
        assert completed.wait(SHORT_TIMEOUT)
        _assert_stale_worker_was_abandoned(stale_results, constructed)

        fresh = mcp_server.get_precise_architecture(
            str(tmp_path),
            mode="definition",
            path="api.py",
            line=1,
            character=4,
            deadline=time.monotonic() + 5,
        )

        _assert_fresh_manager_after_close(fresh, constructed)
    finally:
        release.set()
        completed.wait(2)
        if mcp_server_navigation.NAVIGATION_MANAGER is not None:
            mcp_server.close_navigation_session_manager(time.monotonic() + 5)


def test_run_server_closes_navigation_manager_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    closed: list[float] = []

    class FakeServer:
        def __init__(self, _name: str) -> None:
            pass

        def create_initialization_options(self):
            return {}

        async def run(self, *_args) -> None:
            return None

    class Stdio:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_AVAILABLE", True)
    monkeypatch.setattr("llm_wiki.mcp_server_limits.Server", FakeServer)
    monkeypatch.setattr("llm_wiki.mcp_server_limits.stdio_server", Stdio)
    monkeypatch.setattr("llm_wiki.mcp_server_validation.build_tool_definitions", lambda: [])
    monkeypatch.setattr("llm_wiki.mcp_server_runtime.register_resources", lambda *_args: False)
    monkeypatch.setattr("llm_wiki.mcp_server_runtime.register_tools", lambda *_args: None)
    monkeypatch.setattr(
        "llm_wiki.mcp_server_navigation.close_navigation_session_manager",
        lambda deadline: closed.append(deadline),
    )

    assert mcp_server.run_server() == 0
    assert len(closed) == 1
    assert closed[0] > time.monotonic()
