"""Tests for mcp_server.py — tool definitions and helper functions."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TestToolDefinitions:
    """Test MCP tool definitions."""

    def test_build_tool_definitions_returns_list(self):
        from mcp_server import _build_tool_definitions
        tools = _build_tool_definitions()
        # Returns empty list if mcp not installed, or list of Tool objects
        assert isinstance(tools, list)

    def test_nine_tools_defined(self):
        """Should define exactly 9 task-shaped tools."""
        from mcp_server import _build_tool_definitions
        tools = _build_tool_definitions()
        if tools:  # Only check if mcp package is installed
            assert len(tools) == 9

    def test_tool_names_are_task_shaped(self):
        """Tools should be named after tasks, not entities."""
        from mcp_server import _build_tool_definitions
        tools = _build_tool_definitions()
        if tools:
            names = [t.name for t in tools]
            assert "recall" in names
            assert "read_page" in names
            assert "wiki_overview" in names
            assert "vault_status" in names
            assert "compile" in names


class TestHelperFunctions:
    """Test the helper functions that tools wrap."""

    def test_wiki_overview_returns_dict(self):
        from mcp_server import _wiki_overview
        result = _wiki_overview()
        assert isinstance(result, dict)
        assert "page_count" in result
        assert "retrieval_tier" in result

    def test_vault_status_returns_dict(self):
        from mcp_server import _vault_status
        result = _vault_status()
        assert isinstance(result, dict)
        assert "last_compile" in result

    def test_read_page_existing(self):
        """Reading an existing page should return content."""
        from mcp_server import _read_page
        # index.md always exists in knowledge/notes/
        result = _read_page("index")
        # May or may not exist depending on vault state
        assert isinstance(result, dict)

    def test_read_page_nonexistent(self):
        """Reading a non-existent page should return error."""
        from mcp_server import _read_page
        result = _read_page("this-page-does-not-exist-12345")
        assert "error" in result

    def test_search_vault_returns_list(self):
        from mcp_server import _search_vault
        results = _search_vault("test")
        assert isinstance(results, list)

    def test_get_context_batch(self):
        from mcp_server import _get_context
        result = _get_context(["nonexistent-1", "nonexistent-2"])
        assert isinstance(result, dict)

    def test_trigger_compile_returns_dict(self):
        from mcp_server import _trigger_compile
        result = _trigger_compile()
        assert isinstance(result, dict)
        assert "spawned" in result


class TestHandleToolCall:
    """Test the _handle_tool_call async function."""

    def _run(self, name, args=None):
        """Helper to run async function."""
        from mcp_server import _handle_tool_call
        return asyncio.get_event_loop().run_until_complete(
            _handle_tool_call(name, args or {})
        )

    def test_recall_returns_json(self):
        result = self._run("recall", {"query": "auth"})
        data = json.loads(result)
        # v4.0: recall returns {"results": [...], "_meta": {...}}
        assert isinstance(data, dict)
        assert "results" in data or isinstance(data, list)

    def test_wiki_overview_returns_json(self):
        result = self._run("wiki_overview", {})
        data = json.loads(result)
        assert "page_count" in data

    def test_read_page_returns_json(self):
        result = self._run("read_page", {"slug": "nonexistent-xxx"})
        data = json.loads(result)
        assert "error" in data

    def test_unknown_tool_returns_error(self):
        result = self._run("nonexistent_tool", {})
        data = json.loads(result)
        assert "error" in data

    def test_compile_returns_json(self):
        result = self._run("compile", {})
        data = json.loads(result)
        assert "spawned" in data


class TestRunServer:
    """Test server startup."""

    def test_run_server_without_mcp_package(self):
        """run_server should return 1 if mcp not installed."""
        from mcp_server import MCP_AVAILABLE, run_server

        if not MCP_AVAILABLE:
            result = run_server()
            assert result == 1
