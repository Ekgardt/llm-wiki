"""Tests for mcp_server.py — tool definitions and helper functions."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


ENVELOPE_FIELDS = {
    "schema_version",
    "generated_at",
    "index_timestamp",
    "source_commit",
    "freshness",
    "coverage",
    "confidence",
    "fallback",
    "partial",
    "warnings",
    "data",
}

VALID_TOOL_CALLS = {
    "recall": {"query": "test"},
    "read_page": {"slug": "test"},
    "wiki_overview": {},
    "vault_status": {},
    "get_decisions": {},
    "get_context": {"slugs": []},
    "check_contradiction": {"claim": "test"},
    "log_decision": {"summary": "test"},
    "compile": {},
    "find_dead_code": {"directory": "C:\\project"},
    "get_architecture": {"directory": "C:\\project"},
    "doctor": {},
}

TOOL_HELPERS = {
    "recall": "_search_vault",
    "read_page": "_read_page",
    "wiki_overview": "_wiki_overview",
    "vault_status": "_vault_status",
    "get_decisions": "_get_decisions",
    "get_context": "_get_context",
    "check_contradiction": "_check_contradiction",
    "log_decision": "_log_decision",
    "compile": "_trigger_compile",
    "find_dead_code": "_find_dead_code",
    "get_architecture": "_get_architecture",
    "doctor": "_doctor",
}

MISSING_REQUIRED_CALLS = [
    ("recall", {}),
    ("read_page", {}),
    ("get_context", {}),
    ("check_contradiction", {}),
    ("log_decision", {}),
    ("find_dead_code", {}),
    ("get_architecture", {}),
]

WRONG_TYPE_CALLS = [
    ("recall", {"query": 1}),
    ("recall", {"query": "x", "limit": True}),
    ("read_page", {"slug": []}),
    ("get_decisions", {"query": 1}),
    ("get_decisions", {"limit": "10"}),
    ("get_context", {"slugs": "page"}),
    ("get_context", {"slugs": [1]}),
    ("get_context", {"slugs": [], "include": "frontmatter"}),
    ("get_context", {"slugs": [], "include": [1]}),
    ("check_contradiction", {"claim": 1}),
    ("log_decision", {"summary": 1}),
    ("log_decision", {"summary": "x", "rationale": 1}),
    ("find_dead_code", {"directory": 1}),
    ("get_architecture", {"directory": 1}),
    ("doctor", {"repair": "yes"}),
]


class TestToolDefinitions:
    """Test MCP tool definitions."""

    def test_build_tool_definitions_returns_list(self):
        from mcp_server import _build_tool_definitions
        tools = _build_tool_definitions()
        # Returns empty list if mcp not installed, or list of Tool objects
        assert isinstance(tools, list)

    def test_twelve_tools_defined(self):
        """Should define exactly 12 task-shaped tools."""
        from mcp_server import _build_tool_definitions
        tools = _build_tool_definitions()
        if tools:  # Only check if mcp package is installed
            assert len(tools) == 12

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
            assert "find_dead_code" in names
            assert "get_architecture" in names
            assert "doctor" in names

    def test_doctor_has_optional_repair_boolean(self):
        import mcp_server

        schema = mcp_server.TOOL_INPUT_SCHEMAS["doctor"]
        assert schema["required"] == []
        assert schema["properties"]["repair"]["type"] == "boolean"

    def test_find_dead_code_requires_a_directory(self):
        from mcp_server import _build_tool_definitions

        tools = _build_tool_definitions()
        if tools:
            tool = next(item for item in tools if item.name == "find_dead_code")
            assert tool.inputSchema["required"] == ["directory"]
            assert tool.inputSchema["properties"]["directory"]["type"] == "string"

    def test_get_architecture_requires_a_directory(self):
        from mcp_server import _build_tool_definitions

        tools = _build_tool_definitions()
        if tools:
            tool = next(item for item in tools if item.name == "get_architecture")
            assert tool.inputSchema["required"] == ["directory"]
            assert tool.inputSchema["properties"]["directory"]["type"] == "string"

    def test_tools_advertise_the_envelope_schema_when_supported(self):
        import mcp_server

        tools = mcp_server._build_tool_definitions()
        if tools and mcp_server.MCP_STRUCTURED_OUTPUT_AVAILABLE:
            for tool in tools:
                assert set(tool.outputSchema["required"]) == ENVELOPE_FIELDS

    def test_recall_limit_retains_integer_only_compatibility(self):
        import mcp_server

        limit = mcp_server.TOOL_INPUT_SCHEMAS["recall"]["properties"]["limit"]

        assert limit["type"] == "integer"
        assert "minimum" not in limit
        assert "maximum" not in limit

    def test_limit_and_context_include_descriptions_are_honest(self):
        import mcp_server

        recall_limit = mcp_server.TOOL_INPUT_SCHEMAS["recall"]["properties"]["limit"]
        decision_limit = mcp_server.TOOL_INPUT_SCHEMAS["get_decisions"]["properties"]["limit"]
        include = mcp_server.TOOL_INPUT_SCHEMAS["get_context"]["properties"]["include"]

        assert "clamp" in recall_limit["description"].lower()
        assert "clamp" in decision_limit["description"].lower()
        assert "neighbors" not in include["description"].lower()
        assert "content_preview" in include["description"]


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

    def test_wiki_overview_uses_configured_vault_root(self, tmp_path, monkeypatch):
        import lookup_mode
        import memory_state
        from mcp_server import _wiki_overview

        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        monkeypatch.setattr(lookup_mode, "count_wiki_pages", lambda: 0)
        monkeypatch.setattr(lookup_mode, "tier_for", lambda count: "direct")

        result = _wiki_overview()

        assert result["vault_root"] == str(tmp_path)

    def test_vault_status_wording_is_limited_to_compile_status_and_backlog(self):
        import mcp_server

        assert mcp_server._vault_status.__doc__ == (
            "Return compile status and current daily-file backlog."
        )
        tools = mcp_server._build_tool_definitions()
        if tools:
            tool = next(item for item in tools if item.name == "vault_status")
            assert tool.description == (
                "Get compile status and current daily-file backlog."
            )

    def test_vault_status_counts_changed_daily_files(self, tmp_path, monkeypatch):
        import memory_state
        from mcp_server import _vault_status

        daily = tmp_path / "knowledge" / "daily"
        daily.mkdir(parents=True)
        unchanged = daily / "2026-07-12.md"
        changed = daily / "2026-07-13.md"
        unchanged.write_text("same", encoding="utf-8")
        changed.write_text("new", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        monkeypatch.setattr(
            memory_state,
            "load_state",
            lambda: {
                "compiled_daily_hashes": {
                    unchanged.name: "hash-same",
                    changed.name: "old-hash",
                }
            },
        )
        monkeypatch.setattr(
            memory_state,
            "file_hash",
            lambda path: "hash-same" if path == unchanged else "hash-new",
        )

        assert _vault_status()["compile_backlog"] == 1

    def test_vault_status_counts_unreadable_daily_conservatively(
        self, tmp_path, monkeypatch
    ):
        import memory_state
        from mcp_server import _vault_status

        daily = tmp_path / "knowledge" / "daily"
        daily.mkdir(parents=True)
        unreadable = daily / "2026-07-13.md"
        unreadable.write_text("content", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        monkeypatch.setattr(
            memory_state,
            "load_state",
            lambda: {"compiled_daily_hashes": {unreadable.name: "old"}},
        )

        def fail_hash(path):
            raise OSError("unreadable")

        monkeypatch.setattr(memory_state, "file_hash", fail_hash)

        assert _vault_status()["compile_backlog"] == 1

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

    @pytest.mark.parametrize(
        "slug",
        [
            "../daily/secret",
            "../../README",
            r"..\daily\secret",
            r"C:\Windows\system32\config",
            r"C:relative",
            "/absolute/path",
            ".",
            "..",
        ],
    )
    def test_read_page_rejects_non_flat_and_drive_qualified_slugs(
        self, tmp_path, monkeypatch, slug
    ):
        import memory_state
        from mcp_server import _read_page

        notes = tmp_path / "knowledge" / "notes"
        notes.mkdir(parents=True)
        daily = tmp_path / "knowledge" / "daily"
        daily.mkdir()
        (daily / "secret.md").write_text("secret", encoding="utf-8")
        (tmp_path / "README.md").write_text("readme", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = _read_page(slug)

        assert "error" in result
        assert "invalid" in result["error"].lower()
        assert "secret" not in result.get("content", "")
        assert "readme" not in result.get("content", "")

    def test_read_page_accepts_flat_unicode_and_space_slug(self, tmp_path, monkeypatch):
        import memory_state
        from mcp_server import _read_page

        notes = tmp_path / "knowledge" / "notes"
        notes.mkdir(parents=True)
        (notes / "Проект notes.md").write_text("safe content", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = _read_page("Проект notes")

        assert result["content"] == "safe content"
        assert result["slug"] == "Проект notes"

    def test_read_page_resolves_content_addressed_evidence(self, tmp_path, monkeypatch):
        import memory_state
        from mcp_server import _read_page
        from reliable_memory import sha256_bytes

        daily = tmp_path / "knowledge/daily/2026-01-01.md"
        notes = tmp_path / "knowledge/notes"
        daily.parent.mkdir(parents=True)
        notes.mkdir(parents=True)
        source = b"## [evt-1] event\nverified quote\n"
        daily.write_bytes(source)
        start = source.index(b"verified quote")
        reference = (
            f"daily:2026-01-01 sha256:{sha256_bytes(source)} "
            f"block:evt-1 bytes:{start}-{start + len(b'verified quote')}"
        )
        (notes / "page.md").write_text(f"## Evidence\n- `{reference}`\n", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = _read_page("page")

        assert result["evidence"] == [{
            "reference": reference,
            "sha256": sha256_bytes(b"verified quote"),
            "text": "verified quote",
        }]

    def test_read_page_fails_closed_when_evidence_hash_is_wrong(self, tmp_path, monkeypatch):
        import memory_state
        from mcp_server import _read_page

        daily = tmp_path / "knowledge/daily/2026-01-01.md"
        notes = tmp_path / "knowledge/notes"
        daily.parent.mkdir(parents=True)
        notes.mkdir(parents=True)
        source = b"## [evt-1] event\nverified quote\n"
        daily.write_bytes(source)
        start = source.index(b"verified quote")
        reference = (
            f"daily:2026-01-01 sha256:{'0' * 64} "
            f"block:evt-1 bytes:{start}-{start + len(b'verified quote')}"
        )
        (notes / "page.md").write_text(f"## Evidence\n- `{reference}`\n", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = _read_page("page")

        assert "error" in result
        assert "evidence" in result["error"].lower()

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

    def test_code_tools_reject_empty_missing_file_and_drive_root(self, tmp_path):
        import mcp_server

        missing = tmp_path / "missing"
        regular_file = tmp_path / "file.py"
        regular_file.write_text("", encoding="utf-8")
        drive_root = Path(tmp_path.anchor)

        for value in ("", str(missing), str(regular_file), str(drive_root)):
            for tool in (mcp_server._find_dead_code, mcp_server._get_architecture):
                result = tool(value)
                assert "error" in result

    def test_code_tools_do_not_fall_back_to_cwd(self, monkeypatch):
        import mcp_server

        called = []
        monkeypatch.setattr("code_graph.find_dead_code", lambda directory: called.append(directory))

        result = mcp_server._find_dead_code("")

        assert "error" in result
        assert called == []


class TestHandleToolCall:
    """Test the _handle_tool_call async function."""

    _MISSING = object()

    def _run(self, name, args=_MISSING):
        """Helper to run async function."""
        from mcp_server import _handle_tool_call
        if args is self._MISSING:
            args = {}
        return asyncio.get_event_loop().run_until_complete(
            _handle_tool_call(name, args)
        )

    def _data(self, name, args=None):
        envelope = json.loads(self._run(name, args))
        assert set(envelope) == ENVELOPE_FIELDS
        return envelope["data"]

    def test_recall_returns_json(self):
        data = self._data("recall", {"query": "auth"})
        assert isinstance(data["results"], list)
        assert "_meta" in data

    def test_wiki_overview_returns_json(self):
        data = self._data("wiki_overview", {})
        assert "page_count" in data
        assert "_meta" in data

    def test_read_page_returns_json(self):
        data = self._data("read_page", {"slug": "nonexistent-xxx"})
        assert "error" in data

    def test_unknown_tool_returns_error(self):
        envelope = json.loads(self._run("nonexistent_tool", {}))
        assert "error" in envelope["data"]
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_compile_returns_json(self):
        data = self._data("compile", {})
        assert "spawned" in data

    @pytest.mark.parametrize("tool_name", VALID_TOOL_CALLS)
    def test_every_helper_exception_returns_degraded_envelope(self, monkeypatch, tool_name):
        import mcp_server

        def fail(*args, **kwargs):
            raise RuntimeError(f"{tool_name} failed")

        monkeypatch.setattr(mcp_server, TOOL_HELPERS[tool_name], fail)

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["data"] == {"error": f"{tool_name} failed"}
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    @pytest.mark.parametrize(
        "tool_name",
        ["read_page", "log_decision", "find_dead_code", "get_architecture"],
    )
    def test_helper_error_payloads_are_degraded(self, monkeypatch, tool_name):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            TOOL_HELPERS[tool_name],
            lambda *args, **kwargs: {"error": "helper error"},
        )

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["data"] == {"error": "helper error"}
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0
        assert envelope["confidence"] < 1

    @pytest.mark.parametrize("tool_name, arguments", MISSING_REQUIRED_CALLS)
    def test_missing_required_inputs_are_enveloped_before_dispatch(
        self, monkeypatch, tool_name, arguments
    ):
        import mcp_server

        called = False

        def helper(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(mcp_server, TOOL_HELPERS[tool_name], helper)

        envelope = json.loads(self._run(tool_name, arguments))

        assert "required" in envelope["data"]["error"].lower()
        assert envelope["partial"] is True
        assert called is False

    @pytest.mark.parametrize("tool_name, arguments", WRONG_TYPE_CALLS)
    def test_wrong_input_types_and_bounds_are_enveloped_before_dispatch(
        self, monkeypatch, tool_name, arguments
    ):
        import mcp_server

        called = False

        def helper(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(mcp_server, TOOL_HELPERS[tool_name], helper)

        envelope = json.loads(self._run(tool_name, arguments))

        assert "error" in envelope["data"]
        assert envelope["partial"] is True
        assert called is False

    @pytest.mark.parametrize("arguments", [None, [], "query", 1, True])
    def test_non_object_arguments_are_enveloped(self, arguments):
        envelope = json.loads(self._run("wiki_overview", arguments))

        assert "object" in envelope["data"]["error"].lower()
        assert envelope["partial"] is True

    def test_every_tool_success_path_uses_the_envelope(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "_search_vault", lambda *args, **kwargs: [])
        monkeypatch.setattr(mcp_server, "_read_page", lambda *args, **kwargs: {"ok": True})
        monkeypatch.setattr(mcp_server, "_wiki_overview", lambda: {"ok": True})
        monkeypatch.setattr(mcp_server, "_vault_status", lambda: {"ok": True})
        monkeypatch.setattr(mcp_server, "_get_decisions", lambda *args, **kwargs: [])
        monkeypatch.setattr(mcp_server, "_get_context", lambda *args, **kwargs: {})
        monkeypatch.setattr(mcp_server, "_check_contradiction", lambda *args: [])
        monkeypatch.setattr(mcp_server, "_log_decision", lambda *args: {"ok": True})
        monkeypatch.setattr(mcp_server, "_trigger_compile", lambda: {"ok": True})
        monkeypatch.setattr(mcp_server, "_find_dead_code", lambda *args: {"ok": True})
        monkeypatch.setattr(mcp_server, "_get_architecture", lambda *args: {"ok": True})
        monkeypatch.setattr(mcp_server, "_doctor", lambda *args: {"overall_status": "ok"})
        for name, arguments in VALID_TOOL_CALLS.items():
            envelope = json.loads(self._run(name, arguments))
            assert set(envelope) == ENVELOPE_FIELDS, name

    @pytest.mark.parametrize(
        "tool_name, helper_name",
        [
            ("recall", "_search_vault"),
            ("get_decisions", "_get_decisions"),
            ("check_contradiction", "_check_contradiction"),
        ],
    )
    def test_bm25_only_search_is_marked_as_fallback(
        self, monkeypatch, tool_name, helper_name
    ):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            helper_name,
            lambda *args, **kwargs: [{"path": "page.md", "score": 1.0}],
        )

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["fallback"] is True
        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1
        assert any("bm25" in warning.lower() for warning in envelope["warnings"])

    @pytest.mark.parametrize("tool_name", ["recall", "get_decisions", "check_contradiction"])
    def test_empty_search_results_have_low_coverage(self, monkeypatch, tool_name):
        import mcp_server

        monkeypatch.setattr(
            mcp_server, TOOL_HELPERS[tool_name], lambda *args, **kwargs: []
        )

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["partial"] is True
        assert envelope["coverage"] <= 0.25
        assert envelope["confidence"] < 0.5
        assert envelope["warnings"]

    def test_fused_search_does_not_claim_full_certainty(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_search_vault",
            lambda *args, **kwargs: [{"path": "page.md", "fused_score": 0.1}],
        )

        envelope = json.loads(self._run("recall", {"query": "test"}))

        assert envelope["fallback"] is False
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1

    @pytest.mark.parametrize(
        "limit, effective",
        [(-10, 1), (0, 1), (21, 20), (10_000, 20)],
    )
    def test_recall_accepts_integer_limit_but_clamps_helper(
        self, monkeypatch, limit, effective
    ):
        import mcp_server

        received = []

        def search(query, *, limit):
            received.append(limit)
            return [{"path": "page.md", "fused_score": 0.1}]

        monkeypatch.setattr(mcp_server, "_search_vault", search)

        envelope = json.loads(
            self._run("recall", {"query": "test", "limit": limit})
        )

        assert "error" not in envelope["data"]
        assert received == [effective]
        assert envelope["partial"] is True
        assert any("clamp" in warning.lower() for warning in envelope["warnings"])

    @pytest.mark.parametrize("limit, effective", [(-1, 1), (0, 1), (21, 20), (999, 20)])
    def test_get_decisions_accepts_integer_limit_but_clamps_helper(
        self, monkeypatch, limit, effective
    ):
        import mcp_server

        received = []

        def decisions(query, *, limit):
            received.append(limit)
            return [{"path": "decision.md", "fused_score": 0.1}]

        monkeypatch.setattr(mcp_server, "_get_decisions", decisions)

        envelope = json.loads(self._run("get_decisions", {"limit": limit}))

        assert "error" not in envelope["data"]
        assert received == [effective]
        assert envelope["partial"] is True
        assert any("clamp" in warning.lower() for warning in envelope["warnings"])

    @pytest.mark.parametrize("tool_name", ["recall", "get_decisions", "check_contradiction"])
    @pytest.mark.parametrize("index_state", ["stale", "missing"])
    def test_unfresh_index_degrades_index_dependent_tools(
        self, monkeypatch, tmp_path, tool_name, index_state
    ):
        import mcp_contract
        import mcp_server

        if index_state == "stale":
            index = tmp_path / "cache" / "index.sqlite"
            index.parent.mkdir()
            index.touch()
            old = datetime.now(timezone.utc) - timedelta(days=2)
            os.utime(index, (old.timestamp(), old.timestamp()))
        real_build = mcp_contract.build_envelope
        monkeypatch.setattr(
            mcp_server,
            "build_envelope",
            lambda data, **kwargs: real_build(data, root=tmp_path, **kwargs),
        )
        monkeypatch.setattr(
            mcp_server,
            TOOL_HELPERS[tool_name],
            lambda *args, **kwargs: [{"path": "page.md", "fused_score": 0.1}],
        )
        monkeypatch.setattr(mcp_server, "_meta", lambda: {})

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["freshness"] == index_state.replace("missing", "unknown")
        assert envelope["partial"] is True
        assert any("freshness" in warning.lower() for warning in envelope["warnings"])

    def test_get_context_accepts_unrecognized_include_strings(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_get_context",
            lambda slugs, include: {"include": include},
        )

        envelope = json.loads(
            self._run("get_context", {"slugs": [], "include": ["future-option"]})
        )

        assert envelope["data"]["include"] == ["future-option"]

    def test_contradiction_candidates_are_always_unverified(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_check_contradiction",
            lambda claim: [{"path": "page.md", "fused_score": 0.1}],
        )

        envelope = json.loads(
            self._run("check_contradiction", {"claim": "conflicting claim"})
        )

        assert envelope["partial"] is True
        assert envelope["confidence"] < 0.6
        assert any("unverified" in warning.lower() for warning in envelope["warnings"])

    def test_tool_text_is_strict_json_for_non_finite_helper_data(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server, "_wiki_overview", lambda: {"score": float("nan")}
        )
        monkeypatch.setattr(mcp_server, "_meta", lambda: {})

        text = self._run("wiki_overview", {})

        assert "NaN" not in text
        assert json.loads(text)["data"]["score"] is None

    def test_find_dead_code_returns_candidates(self, tmp_path, monkeypatch):
        expected = [{"name": "unused", "status": "candidate", "graph_complete": False}]
        monkeypatch.setattr("code_graph.find_dead_code", lambda directory: expected)

        envelope = json.loads(self._run("find_dead_code", {"directory": str(tmp_path)}))
        data = envelope["data"]
        assert data["candidates"] == expected
        assert data["directory"] == str(tmp_path.resolve())
        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_get_architecture_returns_summary(self, tmp_path, monkeypatch):
        expected = {
            "entry_points": [{"name": "main"}],
            "routes": [],
            "hotspots": [],
            "communities": [],
            "graph_complete": False,
        }
        monkeypatch.setattr("code_graph.get_architecture", lambda directory: expected)

        envelope = json.loads(self._run("get_architecture", {"directory": str(tmp_path)}))
        data = envelope["data"]
        assert data["architecture"] == expected
        assert data["directory"] == str(tmp_path.resolve())
        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_doctor_returns_uniform_conservative_envelope(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_doctor",
            lambda repair=False: {
                "overall_status": "degraded",
                "checks": [{"id": "index", "status": "degraded", "message": "stale"}],
            },
        )

        envelope = json.loads(self._run("doctor", {"repair": False}))

        assert set(envelope) == ENVELOPE_FIELDS
        assert envelope["data"]["overall_status"] == "degraded"
        assert envelope["partial"] is True
        assert envelope["confidence"] < 1
        assert envelope["warnings"]


class TestResources:
    def test_resource_definitions_degrade_when_sdk_support_is_unavailable(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "MCP_RESOURCES_AVAILABLE", False)

        assert mcp_server._build_resource_definitions() == []

    def test_health_and_context_resource_definitions_when_supported(self):
        import mcp_server

        resources = mcp_server._build_resource_definitions()
        if mcp_server.MCP_RESOURCES_AVAILABLE:
            assert {str(resource.uri) for resource in resources} == {
                "llm-wiki://health",
                "llm-wiki://context",
            }
        else:
            assert resources == []

    def test_health_and_context_handlers_return_enveloped_json(self):
        from mcp_server import _handle_resource_read

        for uri in ("llm-wiki://health", "llm-wiki://context"):
            envelope = json.loads(_handle_resource_read(uri))
            assert set(envelope) == ENVELOPE_FIELDS
            assert isinstance(envelope["data"], dict)

    def test_unknown_health_status_degrades_resource_quality(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda: {
                "last_compile": "never",
                "last_compile_status": "unknown",
                "compile_backlog": 0,
            },
        )

        envelope = json.loads(mcp_server._handle_resource_read("llm-wiki://health"))

        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_compile_backlog_degrades_health_resource(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda: {
                "last_compile": datetime.now(timezone.utc).isoformat(),
                "last_compile_status": "ok",
                "compile_backlog": 2,
            },
        )

        envelope = json.loads(mcp_server._handle_resource_read("llm-wiki://health"))

        assert envelope["partial"] is True
        assert any("backlog" in warning.lower() for warning in envelope["warnings"])

    def test_resource_text_is_strict_json_for_non_finite_data(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda: {
                "last_compile": "never",
                "last_compile_status": "unknown",
                "compile_backlog": float("inf"),
            },
        )

        text = mcp_server._handle_resource_read("llm-wiki://health")

        assert "Infinity" not in text
        assert json.loads(text)["data"]["compile_backlog"] is None

    def test_stale_index_degrades_context_resource_quality(self, monkeypatch, tmp_path):
        import mcp_contract
        import mcp_server

        index = tmp_path / "cache" / "index.sqlite"
        index.parent.mkdir()
        index.touch()
        old = datetime.now(timezone.utc) - timedelta(days=2)
        os.utime(index, (old.timestamp(), old.timestamp()))
        real_build = mcp_contract.build_envelope
        monkeypatch.setattr(
            mcp_server,
            "build_envelope",
            lambda data, **kwargs: real_build(data, root=tmp_path, **kwargs),
        )
        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda: {
                "last_compile": datetime.now(timezone.utc).isoformat(),
                "last_compile_status": "ok",
                "compile_backlog": 0,
            },
        )
        monkeypatch.setattr(mcp_server, "_wiki_overview", lambda: {"page_count": 1})

        envelope = json.loads(mcp_server._handle_resource_read("llm-wiki://context"))

        assert envelope["freshness"] == "stale"
        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["warnings"]

    def test_unknown_resource_returns_enveloped_error(self):
        from mcp_server import _handle_resource_read

        envelope = json.loads(_handle_resource_read("llm-wiki://unknown"))

        assert set(envelope) == ENVELOPE_FIELDS
        assert "error" in envelope["data"]

    def test_resource_registration_degrades_without_sdk_support(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "MCP_RESOURCES_AVAILABLE", False)
        assert mcp_server._register_resources(object()) is False

    def test_resource_registration_exposes_working_callbacks(self, monkeypatch):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callbacks = {}

            def list_resources(self):
                return lambda function: self.callbacks.setdefault("list", function) or function

            def read_resource(self):
                return lambda function: self.callbacks.setdefault("read", function) or function

        server = FakeServer()
        monkeypatch.setattr(mcp_server, "MCP_RESOURCES_AVAILABLE", True)
        monkeypatch.setattr(mcp_server, "Resource", Model)
        monkeypatch.setattr(mcp_server, "TextResourceContents", Model)

        assert mcp_server._register_resources(server) is True
        resources = asyncio.run(server.callbacks["list"]())
        contents = asyncio.run(server.callbacks["read"]("llm-wiki://health"))

        assert {resource.uri for resource in resources} == {
            "llm-wiki://health",
            "llm-wiki://context",
        }
        assert json.loads(contents[0].text)["data"]["last_compile_status"]


class TestCallbackCompatibility:
    @pytest.mark.parametrize(
        ("arguments", "report", "expected_error"),
        [
            (
                {"repair": True},
                {
                    "overall_status": "error",
                    "checks": [
                        {
                            "id": "index",
                            "status": "error",
                            "message": "repair failed",
                            "details": {"repair_errors": ["Index repair failed"]},
                        }
                    ],
                },
                True,
            ),
            (
                {"repair": False},
                {
                    "overall_status": "error",
                    "checks": [
                        {
                            "id": "environment",
                            "status": "error",
                            "message": "missing",
                            "details": {},
                        }
                    ],
                },
                False,
            ),
        ],
    )
    def test_registered_doctor_callback_signals_only_repair_failures(
        self, monkeypatch, arguments, report, expected_error
    ):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callback = None

            def list_tools(self):
                return lambda callback: callback

            def call_tool(self, **kwargs):
                def register(callback):
                    self.callback = callback
                    return callback

                return register

        server = FakeServer()
        monkeypatch.setattr(mcp_server, "TextContent", Model)
        monkeypatch.setattr(mcp_server, "CallToolResult", Model)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        monkeypatch.setattr(mcp_server, "_doctor", lambda repair=False: report)
        mcp_server._register_tools(server, [])

        result = asyncio.run(server.callback("doctor", arguments))

        assert result.isError is expected_error

    def test_call_tool_result_marks_protocol_errors(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeCallToolResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "TextContent", FakeTextContent)
        monkeypatch.setattr(mcp_server, "CallToolResult", FakeCallToolResult)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        text = json.dumps({"schema_version": "1.0", "data": {"error": "bad"}})

        result = mcp_server._format_tool_result(text)

        assert isinstance(result, FakeCallToolResult)
        assert result.content[0].text == text
        assert result.structuredContent == json.loads(text)
        assert result.isError is True

    def test_call_tool_result_marks_success_as_non_error(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeCallToolResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "TextContent", FakeTextContent)
        monkeypatch.setattr(mcp_server, "CallToolResult", FakeCallToolResult)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        text = json.dumps({"schema_version": "1.0", "data": {"ok": True}})

        result = mcp_server._format_tool_result(text)

        assert result.isError is False

    def test_modern_callback_returns_text_and_structured_envelope(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "TextContent", FakeTextContent)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", True)
        text = json.dumps({"schema_version": "1.0", "data": {"ok": True}})

        content, structured = mcp_server._format_tool_result(text)

        assert content[0].text == text
        assert structured == json.loads(text)

    def test_legacy_callback_returns_text_only(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "TextContent", FakeTextContent)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)

        content = mcp_server._format_tool_result('{"data": {}}')

        assert content[0].text == '{"data": {}}'

    def test_registered_modern_callback_disables_sdk_validation_and_envelopes_input(
        self, monkeypatch
    ):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.validate_input = None
                self.callback = None
                self.tools_callback = None

            def list_tools(self):
                def register(callback):
                    self.tools_callback = callback
                    return callback

                return register

            def call_tool(self, *, validate_input=True):
                self.validate_input = validate_input

                def register(callback):
                    self.callback = callback
                    return callback

                return register

        server = FakeServer()
        monkeypatch.setattr(mcp_server, "TextContent", FakeTextContent)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)

        mcp_server._register_tools(server, [])
        content = asyncio.run(server.callback("recall", {}))
        envelope = json.loads(content[0].text)

        assert server.validate_input is False
        assert "required" in envelope["data"]["error"].lower()
        assert envelope["partial"] is True

    def test_registered_legacy_callback_uses_parameterless_decorator(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class LegacyServer:
            def __init__(self):
                self.callback = None

            def list_tools(self):
                return lambda callback: callback

            def call_tool(self):
                def register(callback):
                    self.callback = callback
                    return callback

                return register

        server = LegacyServer()
        monkeypatch.setattr(mcp_server, "TextContent", FakeTextContent)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)

        mcp_server._register_tools(server, [])
        content = asyncio.run(server.callback("read_page", {}))
        envelope = json.loads(content[0].text)

        assert "required" in envelope["data"]["error"].lower()


class TestRunServer:
    """Test server startup."""

    def test_run_server_without_mcp_package(self):
        """run_server should return 1 if mcp not installed."""
        from mcp_server import MCP_AVAILABLE, run_server

        if not MCP_AVAILABLE:
            result = run_server()
            assert result == 1

    def test_core_sdk_remains_available_without_optional_resource_types(self, tmp_path):
        package = tmp_path / "mcp"
        server_package = package / "server"
        server_package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (server_package / "__init__.py").write_text(
            "class Server:\n    pass\n", encoding="utf-8"
        )
        (server_package / "stdio.py").write_text(
            "def stdio_server():\n    return None\n", encoding="utf-8"
        )
        (package / "types.py").write_text(
            "class TextContent:\n    pass\n"
            "class Tool:\n    model_fields = {}\n",
            encoding="utf-8",
        )
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = (
            "import sys; "
            f"sys.path[:0] = [{str(tmp_path)!r}, {str(scripts)!r}]; "
            "import mcp_server; "
            "print(mcp_server.MCP_AVAILABLE, "
            "mcp_server.MCP_STRUCTURED_OUTPUT_AVAILABLE, "
            "mcp_server.MCP_RESOURCES_AVAILABLE)"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "True False False"

    def test_fully_absent_sdk_degrades_without_import_failure(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = (
            "import sys; "
            "sys.modules['mcp'] = None; "
            f"sys.path.insert(0, {str(scripts)!r}); "
            "import mcp_server; "
            "print(mcp_server.MCP_AVAILABLE, "
            "mcp_server.MCP_RESOURCES_AVAILABLE, "
            "mcp_server._build_tool_definitions())"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "False False []"
