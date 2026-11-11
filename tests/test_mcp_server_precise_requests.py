"""Precise architecture requests are validated exactly: position triples, deadlines, modes, paths, scope and source containment."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from tests.mcp_server_support import (
    _assert_positioned_calls_are_precise,
    _assert_structural_calls_are_not_precise,
    _replace_in_every_server_part,
)
from tests.slow_machine import SHORT_TIMEOUT


@pytest.mark.parametrize(
    "mode",
    ["definition", "references", "implementations", "type", "diagnostics"],
)
def test_precise_modes_require_exact_position_triple(mode: str) -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode, "path": "pkg/api.py"},
    )
    assert error is not None
    assert "line" in error or "character" in error


def test_precise_modes_reject_symbol_substitute() -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {
            "directory": "C:/repo",
            "mode": "definition",
            "path": "pkg/api.py",
            "line": 1,
            "character": 0,
            "symbol": "MyClass",
        },
    )
    assert error is not None
    assert "symbol" in error


def test_precise_modes_accept_complete_position() -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {
            "directory": "C:/repo",
            "mode": "definition",
            "path": "pkg/api.py",
            "line": 1,
            "character": 0,
        },
    )
    assert error is None


@pytest.mark.parametrize("mode", ["callers", "callees"])
def test_positioned_calls_require_complete_triple(mode: str) -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode, "path": "pkg/api.py", "line": 1},
    )
    assert error is not None
    assert "character" in error or "together" in error


@pytest.mark.parametrize("mode", ["callers", "callees"])
def test_structural_calls_keep_symbol_behavior(mode: str) -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode, "symbol": "f"},
    )
    assert error is None


def test_navigation_deadline_is_60s_for_precise_modes() -> None:
    import mcp_server

    assert (
        mcp_server.tool_operation_seconds(
            "get_architecture",
            {"mode": "definition", "path": "pkg/api.py", "line": 1, "character": 0},
        )
        == mcp_server.MCP_LSP_STARTUP_SECONDS
    )
    assert (
        mcp_server.tool_operation_seconds(
            "get_architecture",
            {
                "mode": "callers",
                "path": "pkg/api.py",
                "line": 1,
                "character": 0,
            },
        )
        == mcp_server.MCP_LSP_STARTUP_SECONDS
    )


def test_navigation_deadline_is_10s_for_existing_modes() -> None:
    import mcp_server

    assert (
        mcp_server.tool_operation_seconds(
            "get_architecture", {"mode": "summary"}
        )
        == mcp_server.MCP_OPERATION_SECONDS
    )
    assert (
        mcp_server.tool_operation_seconds(
            "get_architecture", {"mode": "callers", "symbol": "f"}
        )
        == mcp_server.MCP_OPERATION_SECONDS
    )
    # recall waits for its reranker on `mcp.retrieval_seconds` (B-9), not the 10 s default.
    assert (
        mcp_server.tool_operation_seconds("recall", {"query": "x"})
        == mcp_server.retrieval_operation_seconds()
    )


def test_precise_request_classification_is_exact() -> None:
    import mcp_server

    _assert_positioned_calls_are_precise(mcp_server)
    _assert_structural_calls_are_not_precise(mcp_server)


def test_precise_architecture_rejects_non_checkout_directory(
    monkeypatch,
) -> None:
    import mcp_server

    def _fail_get(*args, **kwargs):
        raise AssertionError("session manager must not be called for bad directory")

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_session_manager", _fail_get)
    data = mcp_server.get_precise_architecture(
        "not-a-real-directory",
        mode="definition",
        path="pkg/api.py",
        line=1,
        character=0,
        deadline=None,
    )
    assert data["status"] == "error"
    assert data["warnings"] == ("navigation_directory_invalid",)
    assert "error" not in data


def test_structural_callers_not_routed_as_precise(monkeypatch) -> None:
    import mcp_server

    def _fail_precise(*args, **kwargs):
        raise AssertionError("precise routing must not run for structural callers")

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.get_precise_architecture", _fail_precise)
    monkeypatch.setattr("llm_wiki.code_graph_calls.find_callers", lambda *a, **k: {"callers": []})
    resolved = str(Path(__file__).resolve().parent.parent)
    data = mcp_server.get_architecture_mode(
        resolved, mode="callers", symbol="f", deadline=time.monotonic() + SHORT_TIMEOUT
    )
    assert data.get("mode", "callers") == "callers" or "callers" in data


@pytest.mark.parametrize(
    ("arguments", "invalid_field"),
    [
        ({"directory": "C:/repo", "deadline": 1}, "deadline"),
        ({"directory": "C:/repo", "mode": "summary", "symbol": "f"}, "symbol"),
        (
            {"directory": "C:/repo", "mode": "callers", "symbol": "f", "line": 1},
            "line",
        ),
        (
            {
                "directory": "C:/repo",
                "mode": "definition",
                "path": "pkg/api.py",
                "line": 1,
                "character": 0,
                "live": True,
            },
            "live",
        ),
        (
            {
                "directory": "C:/repo",
                "mode": "dependencies",
                "symbol": "f",
                "target": "g",
            },
            "target",
        ),
        (
            {"directory": "C:/repo", "mode": "impact", "live": True},
            "live",
        ),
    ],
)
def test_architecture_validation_rejects_unknown_and_cross_mode_fields(
    arguments: dict[str, object],
    invalid_field: str,
) -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments("get_architecture", arguments)

    assert error is not None
    assert invalid_field in error


@pytest.mark.parametrize(
    "mode",
    ["definition", "references", "implementations", "type", "diagnostics"],
)
def test_exact_modes_list_every_missing_position_field(mode: str) -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode},
    )

    assert error is not None
    assert all(field in error for field in ("path", "line", "character"))


@pytest.mark.parametrize("mode", ["callers", "callees"])
def test_positioned_calls_reject_symbol_even_with_complete_position(mode: str) -> None:
    import mcp_server

    error = mcp_server.validate_tool_arguments(
        "get_architecture",
        {
            "directory": "C:/repo",
            "mode": mode,
            "symbol": "f",
            "path": "pkg/api.py",
            "line": 1,
            "character": 0,
        },
    )

    assert error is not None
    assert "symbol" in error


@pytest.mark.parametrize(
    "arguments",
    [
        {"directory": "C:/repo"},
        {"directory": "C:/repo", "mode": "summary", "live": True},
        {"directory": "C:/repo", "mode": "symbol", "symbol": "f", "live": True},
        {"directory": "C:/repo", "mode": "callers", "symbol": "f"},
        {"directory": "C:/repo", "mode": "callees", "symbol": "f", "live": True},
        {
            "directory": "C:/repo",
            "mode": "dependencies",
            "symbol": "f",
            "reverse": True,
            "live": True,
        },
        {
            "directory": "C:/repo",
            "mode": "path",
            "symbol": "f",
            "target": "g",
            "live": True,
        },
        {"directory": "C:/repo", "mode": "community", "live": True},
        {
            "directory": "C:/repo",
            "mode": "impact",
            "comparison": "two-commits",
            "base": "a",
            "target": "b",
            "branch": "main",
        },
    ],
)
def test_architecture_validation_preserves_every_structural_shape(
    arguments: dict[str, object],
) -> None:
    import mcp_server

    assert mcp_server.validate_tool_arguments("get_architecture", arguments) is None


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "directory": "C:/repo",
            "mode": "definition",
            "path": "../outside.py",
            "line": 1,
            "character": 0,
        },
        {
            "directory": "C:/repo",
            "mode": "definition",
            "path": "pkg/api.py",
            "line": True,
            "character": 0,
        },
        {
            "directory": "C:/repo",
            "mode": "references",
            "path": "pkg/api.py",
            "line": 1,
            "character": 0,
            "offset": True,
        },
        {
            "directory": "C:/repo",
            "mode": "diagnostics",
            "path": "pkg/api.py",
            "line": 1,
            "character": 0,
            "limit": 101,
        },
    ],
)
def test_precise_architecture_rejects_unsafe_paths_bool_ints_and_bounds(
    arguments: dict[str, object],
) -> None:
    import mcp_server

    assert mcp_server.validate_tool_arguments("get_architecture", arguments)


def test_registered_precise_callback_uses_single_sixty_second_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    captured: list[tuple[float, float]] = []

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return 100.0

    class FakeServer:
        callback = None

        def list_tools(self):
            return lambda callback: callback

        def call_tool(self, **_kwargs):
            def register(callback):
                self.callback = callback
                return callback

            return register

    async def bounded(_function, *_args, deadline: float):
        captured.append((_args[-1], deadline))
        return "bounded-result"

    server = FakeServer()
    _replace_in_every_server_part(monkeypatch, "time", Clock)
    monkeypatch.setattr("llm_wiki.mcp_server_workers.run_bounded", bounded)
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
    monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
    monkeypatch.setattr(
        "llm_wiki.mcp_server_limits.TextContent",
        lambda **kwargs: type("Text", (), kwargs)(),
    )
    mcp_server.register_tools(server, [])

    result = asyncio.run(
        server.callback(
            "get_architecture",
            {
                "directory": "C:/repo",
                "mode": "definition",
                "path": "pkg/api.py",
                "line": 1,
                "character": 0,
            },
        )
    )

    assert result == "bounded-result"
    assert captured == [(160.0, 160.0)]


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity semantics")
def test_precise_root_identity_accepts_windows_case_and_separator_variants(
    tmp_path: Path,
) -> None:
    import mcp_server

    variant = Path(str(tmp_path).swapcase().replace("\\", "/"))

    assert mcp_server.same_filesystem_path(tmp_path, variant) is True


def test_precise_scope_receives_same_deadline_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lsp_security
    import mcp_server
    import repository_scope

    source = tmp_path / "api.py"
    source.write_text("def api():\n    pass\n", encoding="utf-8")
    scope = repository_scope.resolve_repository_scope(tmp_path)

    def cancelled() -> bool:
        return False

    captured: list[tuple[float | None, object]] = []

    def resolve_scope(directory, *, deadline=None, cancelled=None):
        assert directory == tmp_path.resolve()
        captured.append((deadline, cancelled))
        return scope

    monkeypatch.setattr(repository_scope, "resolve_repository_scope", resolve_scope)
    monkeypatch.setattr("llm_wiki.mcp_server_workers.operation_cancelled", lambda: cancelled)
    monkeypatch.setattr(
        lsp_security,
        "resolve_repository_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(
        "llm_wiki.mcp_server_navigation.navigation_session_manager",
        lambda *_args, **_kwargs: pytest.fail("manager must follow source containment"),
    )
    deadline = time.monotonic() + 5

    data = mcp_server.get_precise_architecture(
        str(tmp_path),
        mode="definition",
        path="api.py",
        line=1,
        character=0,
        deadline=deadline,
    )

    assert captured == [(deadline, cancelled)]
    assert data["status"] == "error"
    assert data["repository"] == {
        "repository_id": scope.repository_id,
        "checkout_id": scope.checkout_id,
    }


@pytest.mark.parametrize("path", ["missing.py", "../outside.py"])
def test_precise_source_containment_finishes_before_manager_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    import mcp_server

    manager_calls = 0

    def manager(*_args, **_kwargs):
        nonlocal manager_calls
        manager_calls += 1
        raise AssertionError("unsafe source must not create a manager")

    monkeypatch.setattr("llm_wiki.mcp_server_navigation.navigation_session_manager", manager)

    data = mcp_server.get_precise_architecture(
        str(tmp_path),
        mode="definition",
        path=path,
        line=1,
        character=0,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert manager_calls == 0
    assert data["status"] == "error"
    assert "error" not in data
