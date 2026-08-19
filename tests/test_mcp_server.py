"""Tests for mcp_server.py — tool definitions and helper functions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
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
    "components",
    "data",
}

VALID_TOOL_CALLS = {
    "recall": {"query": "test"},
    "read_page": {"slug": "test"},
    "wiki_overview": {},
    "vault_status": {},
    "get_decisions": {},
    "get_context": {"slugs": ["missing"]},
    "check_contradiction": {"claim": "test"},
    "log_decision": {"summary": "test"},
    "compile": {},
    "find_dead_code": {"directory": "C:\\project"},
    "get_architecture": {"directory": "C:\\project"},
    "doctor": {"action": "status"},
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


class DrainAwareSet(set[object]):
    """Signal when a test-owned MCP worker set becomes empty."""

    def __init__(self, drained: threading.Event) -> None:
        super().__init__()
        self._drained = drained

    def discard(self, item: object) -> None:
        super().discard(item)
        if not self:
            self._drained.set()

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
    ("doctor", {"action": "status", "repair": True}),
]


class TestToolDefinitions:
    def test_tool_inventory_remains_exactly_the_canonical_twelve(self):
        import mcp_server

        assert list(mcp_server.TOOL_INPUT_SCHEMAS) == [
            "recall",
            "read_page",
            "wiki_overview",
            "vault_status",
            "get_decisions",
            "get_context",
            "check_contradiction",
            "log_decision",
            "compile",
            "find_dead_code",
            "get_architecture",
            "doctor",
        ]

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

    def test_doctor_has_closed_exact_action_branches(self):
        import mcp_server

        schema = mcp_server.TOOL_INPUT_SCHEMAS["doctor"]
        assert set(schema) == {"type", "oneOf"}
        assert schema["type"] == "object"
        branches = schema["oneOf"]
        assert len(branches) == 9
        assert all(branch["additionalProperties"] is False for branch in branches)
        assert {
            branch["properties"]["action"]["const"] for branch in branches
        } == {
            "status",
            "queue-inspect",
            "queue-cancel",
            "queue-redrive",
            "queue-dead-list",
            "transaction-recover",
            "transaction-undo",
            "archive-status",
            "claim-status",
        }

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"action": "unknown"},
            {"action": "status", "unknown": True},
            {"action": "status", "repair": True},
            {"action": "queue-inspect"},
            {"action": "queue-cancel", "target_id": "task"},
            {"action": "queue-redrive", "target_id": "task", "repair": False},
            {"action": "transaction-recover"},
            {"action": "transaction-undo", "repair": True},
            {"action": "queue-dead-list", "limit": 0},
            {"action": "claim-status", "limit": 101},
        ],
    )
    def test_doctor_rejects_unknown_and_forbidden_argument_combinations(
        self, arguments
    ):
        import mcp_server

        assert mcp_server._validate_tool_arguments("doctor", arguments)

    @pytest.mark.parametrize(
        "arguments",
        [
            {"action": "status"},
            {"action": "status", "limit": 100},
            {"action": "queue-inspect", "target_id": "task"},
            {"action": "queue-cancel", "target_id": "task", "repair": True},
            {"action": "queue-redrive", "target_id": "task", "repair": True},
            {"action": "queue-dead-list", "limit": 1},
            {"action": "transaction-recover", "repair": True, "limit": 10},
            {"action": "transaction-undo", "target_id": "tx", "repair": True},
            {"action": "archive-status", "limit": 10},
            {"action": "claim-status", "limit": 10},
        ],
    )
    def test_doctor_accepts_only_declared_action_shapes(self, arguments):
        import mcp_server

        assert mcp_server._validate_tool_arguments("doctor", arguments) is None

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

    def test_get_architecture_declares_all_canonical_modes_without_new_tool(self):
        import mcp_server

        modes = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]["properties"]["mode"]
        assert modes["enum"] == [
            "summary", "symbol", "callers", "callees", "dependencies",
            "path", "community", "impact", "definition", "references",
            "implementations", "type", "diagnostics",
        ]
        tools = mcp_server._build_tool_definitions()
        if tools:
            assert len(tools) == 12

    def test_code_tools_allow_explicit_live_fallback_without_adding_tools(self):
        import mcp_server

        for name in ("find_dead_code", "get_architecture"):
            live = mcp_server.TOOL_INPUT_SCHEMAS[name]["properties"]["live"]
            assert live == {
                "type": "boolean",
                "default": False,
                "description": "Bypass the active generation and run live extraction",
            }

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

    def test_recall_grounded_schema_matches_qa_profiles_without_adding_a_tool(self):
        import mcp_server
        import retrieval

        recall = mcp_server.TOOL_INPUT_SCHEMAS["recall"]

        assert recall["properties"]["grounded"] == {
            "type": "boolean",
            "default": False,
            "description": "Return a verified evidence-grounded answer",
        }
        assert recall["properties"]["profile"]["enum"] == list(
            retrieval.PROFILES
        )
        assert len(mcp_server.TOOL_INPUT_SCHEMAS) == 12

    def test_importing_mcp_server_does_not_import_query_memory(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                f"sys.path.insert(0, {str(scripts)!r}); "
                "import mcp_server; "
                "assert 'query_memory' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "arguments",
        [
            {"query": "test", "grounded": "true"},
            {"query": "test", "grounded": 1},
            {"query": "test", "grounded": True, "profile": 1},
            {"query": "test", "grounded": True, "profile": "UNKNOWN"},
        ],
    )
    def test_recall_rejects_invalid_grounded_types_and_profile_values(self, arguments):
        import mcp_server

        assert mcp_server._validate_tool_arguments("recall", arguments)

    @pytest.mark.parametrize(
        "arguments",
        [
            {"query": "test", "profile": "BASE"},
            {"query": "test", "grounded": False, "profile": "BASE"},
        ],
    )
    def test_recall_rejects_profile_without_grounded_true(self, arguments):
        import mcp_server

        error = mcp_server._validate_tool_arguments("recall", arguments)

        assert error is not None
        assert "grounded" in error

    def test_limit_and_context_include_descriptions_are_honest(self):
        import mcp_server

        recall_limit = mcp_server.TOOL_INPUT_SCHEMAS["recall"]["properties"]["limit"]
        decision_limit = mcp_server.TOOL_INPUT_SCHEMAS["get_decisions"]["properties"]["limit"]
        include = mcp_server.TOOL_INPUT_SCHEMAS["get_context"]["properties"]["include"]

        assert "clamp" in recall_limit["description"].lower()
        assert "clamp" in decision_limit["description"].lower()
        assert "neighbors" not in include["description"].lower()
        assert "content_preview" in include["description"]

    def test_retrieval_schemas_declare_hard_string_and_array_bounds(self):
        import mcp_server

        schemas = mcp_server.TOOL_INPUT_SCHEMAS
        assert schemas["recall"]["properties"]["query"]["maxLength"] == 8192
        assert schemas["get_decisions"]["properties"]["query"]["maxLength"] == 8192
        assert schemas["read_page"]["properties"]["slug"]["maxLength"] == 255
        slugs = schemas["get_context"]["properties"]["slugs"]
        include = schemas["get_context"]["properties"]["include"]
        assert (slugs["minItems"], slugs["maxItems"], slugs["uniqueItems"]) == (1, 20, True)
        assert slugs["items"]["maxLength"] == 255
        assert (include["maxItems"], include["uniqueItems"]) == (10, True)
        assert include["items"]["maxLength"] == 64


class TestHelperFunctions:
    """Test the helper functions that tools wrap."""

    def test_wiki_overview_returns_dict(self):
        from mcp_server import _wiki_overview
        result = _wiki_overview()
        assert isinstance(result, dict)
        assert "page_count" in result
        assert "retrieval_tier" in result

    def test_check_contradiction_returns_structured_assessment(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_assess_contradiction_text",
            lambda claim: {
                "assessments": [],
                "evidence": [],
                "validity": {"status": "unverified"},
                "recommendations": ["quarantine"],
            },
        )
        result = mcp_server._check_contradiction("plain string claim")
        assert set(result) == {
            "assessments",
            "evidence",
            "validity",
            "recommendations",
        }

    def test_real_string_assessment_retrieves_claim_index_and_reports_unsupported_evidence(
        self, tmp_path, monkeypatch
    ):
        import contradiction_pipeline
        import mcp_server
        import memory_state

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        (vault / "knowledge/notes").mkdir(parents=True)
        state.mkdir()
        monkeypatch.setattr(memory_state, "ROOT", vault)
        monkeypatch.setattr(memory_state, "STATE_ROOT", state)
        monkeypatch.setattr(
            contradiction_pipeline,
            "default_secondary_search",
            lambda root, query, limit: [
                {"path": "knowledge/notes/context.md", "title": "Context", "snippet": "project state blue"}
            ],
        )

        result = mcp_server._assess_contradiction_text("project has-state red")

        assert result["validity"]["status"] == "unsupported-evidence"
        assert result["recommendations"] == ["quarantine"]
        assert result["evidence"][0]["retrieval_only"] is True
        quality = mcp_server._quality_for("check_contradiction", result)
        assert quality["partial"] is True
        assert any("unsupported" in item.lower() for item in quality["warnings"])

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

    def test_read_page_rejects_malformed_evidence_and_oversized_pages(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        import memory_state

        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "page.md"
        page.write_text("`daily:2026-01-01 broken`", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        assert "error" in mcp_server._read_page("page")

        page.write_bytes(b"x" * (mcp_server.MAX_MCP_PAGE_BYTES + 1))
        result = mcp_server._read_page("page")
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_read_page_redacts_read_and_evidence_exception_details(
        self, tmp_path, monkeypatch
    ):
        import evidence_resolver
        import mcp_server
        import memory_state

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        (notes / "page.md").write_text("evidence", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        monkeypatch.setattr(
            mcp_server,
            "read_stable_bytes",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sensitive)),
        )
        read_error = mcp_server._read_page("page")

        monkeypatch.setattr(mcp_server, "read_stable_bytes", lambda *args, **kwargs: b"evidence")
        monkeypatch.setattr(
            evidence_resolver,
            "extract_evidence_references",
            lambda content: ["reference"],
        )
        monkeypatch.setattr(
            evidence_resolver.EvidenceResolver,
            "resolve",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sensitive)),
        )
        evidence_error = mcp_server._read_page("page")

        for result in (read_error, evidence_error):
            encoded = json.dumps(result)
            assert sensitive not in encoded
            assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
            assert r"C:\private\vault" not in encoded
            assert result["error"].endswith("operation_failed")

    def test_log_decision_redacts_append_exception_details(self, monkeypatch):
        import daily_log_append
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        monkeypatch.setattr(
            daily_log_append,
            "append_daily",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sensitive)),
        )

        result = mcp_server._log_decision("decision")

        encoded = json.dumps(result)
        assert sensitive not in encoded
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
        assert r"C:\private\vault" not in encoded
        assert result == {"error": "operation_failed"}

    def test_log_decision_propagates_deadline_and_cancellation_to_append(
        self, monkeypatch
    ):
        import daily_log_append
        import mcp_server

        received = []

        def cancelled():
            return False

        monkeypatch.setattr(mcp_server, "_operation_cancelled", lambda: cancelled)
        monkeypatch.setattr(
            daily_log_append,
            "append_daily",
            lambda *args, **kwargs: received.append(kwargs) or Path("daily.md"),
        )
        deadline = time.monotonic() + 5

        result = mcp_server._log_decision("decision", deadline=deadline)

        assert result["status"] == "logged"
        assert received == [{"deadline": deadline, "cancelled": cancelled}]

    def test_read_page_caps_each_resolved_evidence_slice(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state
        from reliable_memory import sha256_bytes

        notes = tmp_path / "knowledge/notes"
        daily = tmp_path / "knowledge/daily/2026-01-01.md"
        notes.mkdir(parents=True)
        daily.parent.mkdir(parents=True)
        quote = b"x" * (mcp_server.MAX_MCP_EVIDENCE_BYTES + 1)
        source = b"## [evt-1] event\n" + quote + b"\n"
        daily.write_bytes(source)
        start = source.index(quote)
        reference = (
            f"daily:2026-01-01 sha256:{sha256_bytes(source)} block:evt-1 "
            f"bytes:{start}-{start + len(quote)}"
        )
        (notes / "page.md").write_text(f"`{reference}`", encoding="utf-8")
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = mcp_server._read_page("page")

        assert "error" in result
        assert "slice exceeds" in result["error"]

    def test_read_page_rejects_symlink_or_reparse_page(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state

        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        page = notes / "page.md"
        try:
            os.symlink(outside, page)
        except OSError:
            page.write_text("safe", encoding="utf-8")
            original = Path.lstat

            def reparse(path: Path):
                result = original(path)
                if path == page:
                    return type(
                        "ReparseStat",
                        (),
                        {
                            "st_mode": result.st_mode,
                            "st_file_attributes": 0x400,
                            "st_size": result.st_size,
                            "st_dev": result.st_dev,
                            "st_ino": result.st_ino,
                            "st_mtime_ns": result.st_mtime_ns,
                        },
                    )()
                return result

            monkeypatch.setattr(Path, "lstat", reparse)
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = mcp_server._read_page("page")

        assert "error" in result
        assert "regular" in result["error"] or "symlink" in result["error"]

    def test_search_vault_returns_list(self):
        from mcp_server import _search_vault
        results = _search_vault("test")
        assert isinstance(results, list)

    def test_search_vault_degrades_after_delayed_optional_initialization(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = []

        def delayed_search(query, **kwargs):
            calls.append((query, kwargs))
            if len(calls) == 1:
                time.sleep(0.01)
                raise TimeoutError("retrieval deadline exceeded")
            return [
                {
                    "path": "knowledge/notes/test.md",
                    "requested_mode": "BASE",
                    "effective_mode": "BASE",
                    "signals_used": ["lexical"],
                    "fallback_reason": None,
                    "generation": "legacy",
                    "partial": False,
                }
            ]

        monkeypatch.setattr(search_memory, "search", delayed_search)

        results = mcp_server._search_vault("test")

        assert len(calls) == 2
        assert calls[0][1]["semantic"] is True
        assert calls[1][1]["semantic"] is False
        assert calls[1][1]["graph"] is False
        assert calls[1][1]["rerank"] is False
        assert calls[1][1]["deadline_monotonic"] == calls[0][1]["deadline_monotonic"]
        assert results[0]["requested_mode"] == "HYBRID"
        assert results[0]["effective_mode"] == "BASE"
        assert results[0]["signals_used"] == ["lexical"]
        assert results[0]["fallback_reason"] == "retrieval_deadline_exceeded"
        assert results[0]["partial"] is True
        trace = mcp_server._retrieval_trace("test", results)
        assert trace["fallback_reason"] == "retrieval_deadline_exceeded"
        assert trace["partial"] is True

    def test_search_vault_reuses_caller_operation_deadline(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = []

        def search(_query, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError("semantic timeout")
            return []

        monkeypatch.setattr(search_memory, "search", search)
        deadline = time.monotonic() + 5

        mcp_server._search_vault("query", deadline=deadline)

        assert len(calls) == 2
        assert [call["deadline_monotonic"] for call in calls] == [deadline, deadline]

    def test_search_vault_propagates_second_timeout(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = 0

        def search(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError(f"attempt {calls} timed out")

        monkeypatch.setattr(search_memory, "search", search)

        with pytest.raises(TimeoutError, match="attempt 2"):
            mcp_server._search_vault("query", deadline=time.monotonic() + 5)

    def test_search_vault_preserves_hybrid_signals_completed_under_deadline(
        self, monkeypatch
    ):
        import mcp_server
        import search_memory

        expected = [{
            "candidate_id": "dense",
            "path": "dense.md",
            "requested_mode": "HYBRID",
            "effective_mode": "HYBRID",
            "signals_used": ["lexical", "dense"],
            "fallback_reason": None,
            "generation": "legacy",
            "partial": False,
        }]
        calls = []

        def search(*_args, **kwargs):
            calls.append(kwargs)
            return expected

        monkeypatch.setattr(search_memory, "search", search)

        rows = mcp_server._search_vault(
            "query", deadline=time.monotonic() + 5
        )

        assert rows == expected
        assert len(calls) == 1
        assert calls[0]["semantic"] is True

    def test_grounded_candidate_retrieval_preserves_hybrid_trace_under_deadline(
        self, monkeypatch
    ):
        import query_memory
        import retrieval

        expected = [{"candidate_id": "dense", "signals_used": ["lexical", "dense"]}]
        captured = []

        def retrieve(*_args, **kwargs):
            captured.append(kwargs)
            return expected

        monkeypatch.setattr(retrieval, "retrieve_via_search_memory", retrieve)
        deadline = time.monotonic() + 5

        rows = query_memory._default_candidates(
            "question", profile="HYBRID", deadline=deadline
        )

        assert rows == tuple(expected)
        assert captured[0]["deadline_monotonic"] == deadline
        assert captured[0]["semantic"] is True

    def test_get_context_batch(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state

        (tmp_path / "knowledge/notes").mkdir(parents=True)
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        result = mcp_server._get_context(["nonexistent-1", "nonexistent-2"])

        assert result["repo_map"] == []
        assert result["missing_slugs"] == ["nonexistent-1", "nonexistent-2"]

    def test_get_context_returns_one_bounded_compiler_package(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state

        notes = tmp_path / "knowledge/notes"
        projects = tmp_path / "knowledge/projects/demo"
        notes.mkdir(parents=True)
        projects.mkdir(parents=True)
        (notes / "choice.md").write_text(
            "---\ntype: decision\nstatus: active\n---\n# Choice\n\n"
            "One-sentence summary: Keep one package.\n\n## Evidence\n\nproof\n",
            encoding="utf-8",
        )
        (notes / "incident.md").write_text(
            "---\ntype: debugging\nstatus: active\n---\n# Incident\n\n"
            "One-sentence summary: Compiler regression.\n",
            encoding="utf-8",
        )
        (projects / "state.md").write_text(
            "---\ntype: project-state\nstatus: active\n---\n# Demo\n\n"
            "One-sentence summary: Active Task17.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        package = mcp_server._get_context(
            ["choice", "incident", "state"], token_budget=1200
        )

        assert set(package) >= {
            "text", "packed_tokens", "token_budget", "repo_map", "pages",
            "symbols", "decisions", "incidents", "active_task", "evidence",
            "retrieval_trace", "materialization_trace",
        }
        assert package["packed_tokens"] <= package["token_budget"] == 1200
        assert package["decisions"]
        assert package["incidents"]
        assert package["active_task"]

    def test_get_context_repo_map_includes_selected_pages_dropped_by_budget(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        import memory_state

        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        for slug in ("alpha", "beta"):
            (notes / f"{slug}.md").write_text(
                "---\ntype: concept\nstatus: active\n---\n"
                f"# {slug.title()}\n\nOne-sentence summary: " + (slug * 300) + "\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)

        package = mcp_server._get_context(["alpha", "beta"], token_budget=256)

        assert package["repo_map"] == [
            "knowledge/notes/alpha.md",
            "knowledge/notes/beta.md",
        ]

    def test_get_context_direct_call_deduplicates_and_enforces_bounds(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        import memory_state

        (tmp_path / "knowledge/notes").mkdir(parents=True)
        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        reads = []
        monkeypatch.setattr(
            mcp_server,
            "_read_page",
            lambda slug, **kwargs: reads.append(slug) or {"slug": slug, "content": ""},
        )

        package = mcp_server._get_context(["first", "first", "second"])
        assert package["missing_slugs"] == ["first", "second"]
        assert reads == []
        with pytest.raises(ValueError, match="slugs"):
            mcp_server._get_context([f"page-{index}" for index in range(21)])
        with pytest.raises(ValueError, match="slug"):
            mcp_server._get_context(["x" * 256])
        with pytest.raises(ValueError, match="include"):
            mcp_server._get_context(["page"], ["x" * 65])

    def test_recall_and_decisions_pass_distinct_source_tools(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = []
        monkeypatch.setattr(
            search_memory,
            "search",
            lambda query, **kwargs: calls.append((query, kwargs)) or [],
        )

        assert mcp_server._search_vault("secret", 2) == []
        assert mcp_server._get_decisions("choice", 3) == []
        assert calls[0][1]["source_tool"] == "mcp.recall"
        assert calls[0][1]["semantic"] is True
        assert calls[0][1]["deadline_monotonic"] > time.monotonic()
        assert calls[1][1]["source_tool"] == "mcp.get_decisions"
        assert calls[1][1]["emit_telemetry"] is False

    def test_get_decisions_records_only_filtered_final_candidates(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry
        import search_memory

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        monkeypatch.setattr(
            search_memory,
            "search",
            lambda query, **kwargs: [
                {"path": "knowledge/notes/concept.md", "type": "concept"},
                {"path": "knowledge/notes/first-decision.md", "type": "decision"},
                {"path": "knowledge/notes/second-decision.md", "type": "decision"},
            ],
        )

        results = mcp_server._get_decisions("private decision query", 10)

        assert [Path(result["path"]).stem for result in results] == [
            "first-decision", "second-decision"
        ]
        rows = list(reversed(retrieval_telemetry.read_events(limit=10, db_path=database)))
        assert [(row.candidate_id, row.rank) for row in rows] == [
            ("first-decision", 1), ("second-decision", 2)
        ]
        assert all(row.retrieval_mode == "decision-filter" for row in rows)
        assert all(row.source_tool == "mcp.get_decisions" for row in rows)
        assert b"private decision query" not in database.read_bytes()

    def test_get_decisions_empty_result_emits_no_events(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry
        import search_memory

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        monkeypatch.setattr(search_memory, "search", lambda query, **kwargs: [])

        assert mcp_server._get_decisions("none", 10) == []
        assert retrieval_telemetry.read_events(limit=10, db_path=database) == []

    def test_read_page_emits_page_and_evidence_events_without_content(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state
        import retrieval_telemetry
        from reliable_memory import sha256_bytes

        notes = tmp_path / "vault/knowledge/notes"
        daily = tmp_path / "vault/knowledge/daily/2026-01-01.md"
        notes.mkdir(parents=True)
        daily.parent.mkdir(parents=True)
        quote = b"distinctive evidence content secret"
        source = b"## [evt-1] event\n" + quote + b"\n"
        daily.write_bytes(source)
        start = source.index(quote)
        reference = (
            f"daily:2026-01-01 sha256:{sha256_bytes(source)} block:evt-1 "
            f"bytes:{start}-{start + len(quote)}"
        )
        (notes / "page.md").write_text(f"# Page\n`{reference}`\n", encoding="utf-8")
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(memory_state, "ROOT", tmp_path / "vault")
        monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path / "state")
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)

        result = mcp_server._read_page("page")

        assert result["slug"] == "page"
        rows = retrieval_telemetry.read_events(limit=10, db_path=database)
        assert {(row.event_kind, row.source_tool) for row in rows} == {
            ("page_read", "mcp.read_page"),
            ("evidence_read", "mcp.read_page"),
        }
        evidence_event = next(row for row in rows if row.event_kind == "evidence_read")
        assert evidence_event.candidate_id == sha256_bytes(quote)
        assert quote not in database.read_bytes()

    def test_context_emits_injection_without_duplicate_page_read(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state
        import retrieval_telemetry

        notes = tmp_path / "vault/knowledge/notes"
        notes.mkdir(parents=True)
        (notes / "page.md").write_text("# Context Page\n", encoding="utf-8")
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(memory_state, "ROOT", tmp_path / "vault")
        monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path / "state")
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)

        package = mcp_server._get_context(["page"])
        assert "knowledge/notes/page.md" in package["repo_map"]

        rows = retrieval_telemetry.read_events(limit=10, db_path=database)
        assert [(row.event_kind, row.source_tool) for row in rows] == [
            ("context_injected", "mcp.get_context")
        ]

    def test_failed_mcp_reads_emit_no_success_events(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state
        import retrieval_telemetry

        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(memory_state, "ROOT", tmp_path / "vault")
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)

        assert "error" in mcp_server._read_page("missing")
        assert retrieval_telemetry.read_events(limit=10, db_path=database) == []

    def test_trigger_compile_returns_dict(self, monkeypatch):
        import compile_memory
        from mcp_server import _trigger_compile

        monkeypatch.setattr(compile_memory, "run_pending_compile", lambda **kwargs: 0)
        result = _trigger_compile()
        assert isinstance(result, dict)

    def test_trigger_compile_runs_under_handler_deadline_and_cancellation(
        self, monkeypatch
    ):
        import compile_memory
        import maybe_compile
        import mcp_server

        received = []

        def cancelled():
            return False

        monkeypatch.setattr(mcp_server, "_operation_cancelled", lambda: cancelled)
        monkeypatch.setattr(
            compile_memory,
            "run_pending_compile",
            lambda **kwargs: received.append(kwargs) or 0,
            raising=False,
        )
        monkeypatch.setattr(
            maybe_compile,
            "spawn_compile_if_idle",
            lambda: pytest.fail("MCP compile must not detach unbounded work"),
        )
        deadline = time.monotonic() + 5

        result = mcp_server._trigger_compile(deadline=deadline)

        assert result == {"status": "completed", "returncode": 0}
        assert received == [
            {"trigger": "manual", "deadline": deadline, "cancelled": cancelled}
        ]

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

    def test_code_tool_helpers_forward_store_report(self, tmp_path, monkeypatch):
        import mcp_server

        report = {
            "source_generation": "gen-25",
            "graph_complete": False,
            "unresolved_count": 2,
            "fallback": False,
        }
        monkeypatch.setattr(
            "code_graph.find_dead_code",
            lambda directory, **options: {"candidates": [{"name": "unused"}], **report},
        )
        monkeypatch.setattr(
            "code_graph.get_architecture",
            lambda directory, **options: {
                "entry_points": [],
                "routes": [],
                "hotspots": [],
                "communities": [],
                **report,
            },
        )

        dead = mcp_server._find_dead_code(str(tmp_path))
        architecture = mcp_server._get_architecture(str(tmp_path))

        assert dead["source_generation"] == "gen-25"
        assert dead["unresolved_count"] == 2
        assert dead["candidates"] == [{"name": "unused"}]
        assert architecture["source_generation"] == "gen-25"
        assert architecture["architecture"]["graph_complete"] is False

    def test_code_tool_helpers_forward_explicit_live_fallback(self, tmp_path, monkeypatch):
        import mcp_server

        seen = []
        monkeypatch.setattr(
            "code_graph.find_dead_code",
            lambda directory, **options: seen.append(options) or [],
        )

        mcp_server._find_dead_code(str(tmp_path), live=True)

        assert seen == [{"live": True, "with_report": True}]


class TestHandleToolCall:
    """Test the _handle_tool_call async function."""

    _MISSING = object()

    def _run(self, name, args=_MISSING):
        """Helper to run async function."""
        from mcp_server import _handle_tool_call
        if args is self._MISSING:
            args = {}
        return asyncio.run(_handle_tool_call(name, args))

    def _data(self, name, args=None):
        envelope = json.loads(self._run(name, args))
        assert set(envelope) == ENVELOPE_FIELDS
        return envelope["data"]

    def test_recall_returns_json(self):
        data = self._data("recall", {"query": "auth"})
        assert isinstance(data["results"], list)
        assert set(data["retrieval_trace"]) == {
            "schema_version", "requested_mode", "effective_mode", "signals_used",
            "fallback_reason", "corpus_generation", "partial", "reranker_applied",
            "reranker_model_id", "reranker_model_revision", "reranker_depth",
            "reranker_duration_ms", "reranker_fallback_reason",
        }
        assert "_meta" in data

    @pytest.mark.parametrize("arguments", [{"query": "auth"}, {"query": "auth", "grounded": False}])
    def test_recall_defaults_to_current_search(self, monkeypatch, arguments):
        import mcp_server
        import query_memory

        searches = []
        monkeypatch.setattr(
            mcp_server,
            "_search_vault",
            lambda query, *, limit: searches.append((query, limit)) or [],
        )
        monkeypatch.setattr(
            query_memory,
            "grounded_qa",
            lambda *args, **kwargs: pytest.fail("grounded QA must be opt-in"),
        )
        monkeypatch.setattr(mcp_server, "_meta", lambda: {})

        envelope = json.loads(self._run("recall", arguments))

        assert searches == [("auth", 8)]
        assert "results" in envelope["data"]

    def test_grounded_recall_calls_direct_api_with_root_profile_and_mcp_deadline(
        self, monkeypatch, tmp_path
    ):
        import mcp_server
        import memory_state
        import query_memory

        answer = {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": "Alpha is enabled.", "citation_ids": ["E1"]}],
            "citations": [{"citation_id": "E1"}],
            "reason": None,
        }
        calls = []

        def grounded_qa(question, **kwargs):
            calls.append((question, kwargs))
            return answer

        monkeypatch.setattr(memory_state, "ROOT", tmp_path)
        monkeypatch.setattr(query_memory, "grounded_qa", grounded_qa)
        monkeypatch.setattr(
            query_memory,
            "answer",
            lambda *args, **kwargs: pytest.fail("CLI answer wrapper must not be used"),
        )
        monkeypatch.setattr(
            mcp_server,
            "_search_vault",
            lambda *args, **kwargs: pytest.fail("search response must not wrap grounded QA"),
        )
        started = time.monotonic()

        envelope = json.loads(
            self._run(
                "recall",
                {"query": "Is alpha enabled?", "grounded": True, "profile": "EXACT"},
            )
        )

        assert envelope["data"] == answer
        assert calls[0][0] == "Is alpha enabled?"
        assert calls[0][1]["vault"] == tmp_path
        assert calls[0][1]["profile"] == "EXACT"
        assert calls[0][1]["deadline"] > started
        assert envelope["partial"] is False
        assert all("abstain" not in warning.lower() for warning in envelope["warnings"])
        assert envelope["coverage"] == 0
        assert 0 < envelope["confidence"] <= 0.8
        assert any("coverage is unknown" in warning.lower() for warning in envelope["warnings"])
        assert envelope["components"] == {}

    @pytest.mark.parametrize(
        "status",
        ["insufficient_evidence", "conflicting_evidence", "unsupported_time_scope"],
    )
    def test_grounded_recall_abstention_is_valid_partial_with_reason_warning(
        self, monkeypatch, status
    ):
        import query_memory

        reason = f"Cannot answer because status is {status}."
        monkeypatch.setattr(
            query_memory,
            "grounded_qa",
            lambda *args, **kwargs: {
                "schema_version": "grounded-answer/v1",
                "status": status,
                "claims": [],
                "citations": [],
                "reason": reason,
            },
        )

        envelope = json.loads(
            self._run("recall", {"query": "question", "grounded": True})
        )

        assert envelope["data"]["status"] == status
        assert "error" not in envelope["data"]
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0
        assert envelope["confidence"] == 0
        assert reason in envelope["warnings"]
        assert envelope["components"] == {}

    @pytest.mark.parametrize(
        "sensitive",
        [
            "api_key=sk-abcdefghijklmnopqrstuvwxyz123456",
            r"C:\Users\operator\private\vault\knowledge\notes\secret.md",
            "PROMPT_SENTINEL do not reveal this user question",
        ],
    )
    def test_grounded_recall_exception_text_is_safe_and_bounded(
        self, monkeypatch, sensitive
    ):
        import query_memory

        def fail(*args, **kwargs):
            raise query_memory.GroundedQAError((sensitive + " ") * 100)

        monkeypatch.setattr(query_memory, "grounded_qa", fail)

        envelope = json.loads(
            self._run("recall", {"query": "question", "grounded": True})
        )

        error = envelope["data"]["error"]
        assert sensitive not in json.dumps(envelope)
        assert len(error) <= 256
        assert all(len(warning) <= 256 for warning in envelope["warnings"])
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0
        assert error in envelope["warnings"]

    @pytest.mark.parametrize("failure", ["provider failed", "schema validation failed"])
    def test_grounded_recall_provider_and_schema_failures_use_error_envelope(
        self, monkeypatch, failure
    ):
        import query_memory

        monkeypatch.setattr(
            query_memory,
            "grounded_qa",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                query_memory.GroundedQAError(failure)
            ),
        )

        envelope = json.loads(
            self._run("recall", {"query": "question", "grounded": True})
        )

        assert "error" in envelope["data"]
        assert failure not in json.dumps(envelope)
        assert envelope["partial"] is True

    def test_grounded_deadline_starts_at_handler_entry_before_validation(self, monkeypatch):
        import mcp_server
        import query_memory

        real_validate = mcp_server._validate_tool_arguments
        deadlines = []
        now = [100.0]

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        def delayed_validate(name, arguments):
            now[0] = 100.03
            return real_validate(name, arguments)

        def grounded_qa(*args, **kwargs):
            deadlines.append(kwargs["deadline"])
            return {
                "schema_version": "grounded-answer/v1",
                "status": "insufficient_evidence",
                "claims": [],
                "citations": [],
                "reason": "No evidence.",
            }

        monkeypatch.setattr(mcp_server, "time", Clock)
        monkeypatch.setattr(mcp_server, "_validate_tool_arguments", delayed_validate)
        monkeypatch.setattr(query_memory, "grounded_qa", grounded_qa)
        handler_start = now[0]

        self._run("recall", {"query": "question", "grounded": True})

        assert deadlines[0] <= handler_start + mcp_server.MCP_OPERATION_SECONDS

    def test_recall_second_timeout_uses_normal_error_envelope(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_search_vault",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("second retrieval timeout")
            ),
        )

        envelope = json.loads(self._run("recall", {"query": "needle"}))

        assert envelope["data"] == {"error": "operation_timeout"}
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0

    def test_impact_reuses_handler_operation_deadline(self, monkeypatch):
        import mcp_server

        captured = []
        monkeypatch.setattr(
            mcp_server,
            "_analyze_impact",
            lambda **kwargs: captured.append(kwargs) or {"changes": []},
        )

        self._run(
            "get_architecture",
            {"directory": ".", "mode": "impact", "comparison": "dirty"},
        )

        assert captured[0]["deadline"] is not None
        assert captured[0]["deadline"] <= time.monotonic() + mcp_server.MCP_OPERATION_SECONDS

    @pytest.mark.parametrize("tool_name", VALID_TOOL_CALLS)
    def test_every_tool_receives_the_single_absolute_handler_deadline(
        self, monkeypatch, tool_name
    ):
        import mcp_server

        expected_data = {
            "recall": [],
            "read_page": {"ok": True},
            "wiki_overview": {"ok": True},
            "vault_status": {"ok": True},
            "get_decisions": [],
            "get_context": {},
            "check_contradiction": {
                "assessments": [],
                "evidence": [],
                "validity": {"status": "unverified"},
                "recommendations": ["quarantine"],
            },
            "log_decision": {"ok": True},
            "compile": {"ok": True},
            "find_dead_code": {"ok": True},
            "get_architecture": {"ok": True},
            "doctor": {"overall_status": "ok"},
        }
        seen = []

        class Clock:
            @staticmethod
            def monotonic():
                return 100.0

        def helper(*args, deadline, **kwargs):
            seen.append(deadline)
            return expected_data[tool_name]

        monkeypatch.setattr(mcp_server, "time", Clock)
        monkeypatch.setattr(mcp_server, TOOL_HELPERS[tool_name], helper)
        monkeypatch.setattr(mcp_server, "_meta", lambda **kwargs: {})

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert "error" not in envelope["data"]
        assert seen == [100.0 + mcp_server.MCP_OPERATION_SECONDS]

    def test_get_context_does_not_replace_the_handler_deadline(self, monkeypatch):
        import corpus_snapshot
        import mcp_server

        seen = []
        now = [200.0]

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        real_validate = mcp_server._validate_tool_arguments

        def delayed_validate(name, arguments):
            now[0] = 205.0
            return real_validate(name, arguments)

        def collect(*args, deadline, **kwargs):
            seen.append(deadline)
            raise TimeoutError("deadline reached")

        monkeypatch.setattr(mcp_server, "time", Clock)
        monkeypatch.setattr(mcp_server, "_validate_tool_arguments", delayed_validate)
        monkeypatch.setattr(corpus_snapshot, "collect_corpus", collect)

        envelope = json.loads(self._run("get_context", {"slugs": ["page"]}))

        assert seen == [200.0 + mcp_server.MCP_OPERATION_SECONDS]
        assert envelope["data"] == {"error": "operation_timeout"}

    def test_blocking_tool_does_not_block_event_loop_and_times_out(self, monkeypatch):
        import mcp_server

        release = threading.Event()
        finished = threading.Event()
        drained = threading.Event()
        workers = DrainAwareSet(drained)

        async def exercise():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def blocked(*, deadline):
                del deadline
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait(60.0)
                finally:
                    finished.set()
                return {"ok": True}

            monkeypatch.setattr(mcp_server, "_vault_status", blocked)
            task = asyncio.create_task(mcp_server._handle_tool_call("vault_status", {}))
            try:
                await asyncio.wait_for(started.wait(), timeout=0.5)
                await asyncio.sleep(0.01)
                loop_progressed = not finished.is_set()
                return loop_progressed, await task
            finally:
                release.set()
                assert finished.wait(1.0)
                assert drained.wait(1.0)
                with mcp_server._MCP_WORKERS_LOCK:
                    assert not workers

        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", workers)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
        loop_progressed, text = asyncio.run(exercise())

        envelope = json.loads(text)
        assert loop_progressed is True
        assert envelope["data"] == {"error": "operation_timeout"}
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0

        expected = {"last_compile": "fresh", "last_compile_status": "ok"}
        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda *, deadline: expected,
        )
        sentinel = json.loads(
            asyncio.run(mcp_server._handle_tool_call("vault_status", {}))
        )
        assert sentinel["data"] == expected
        with mcp_server._MCP_WORKERS_LOCK:
            assert not workers

    def test_timed_out_tool_workers_never_exceed_bounded_slots(self, monkeypatch):
        import threading

        import mcp_server

        release = threading.Event()
        all_done = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def blocked(*, deadline):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                release.wait(60.0)
            finally:
                with lock:
                    active -= 1
                    if active == 0:
                        all_done.set()
            return {"ok": True}

        async def exercise():
            return await asyncio.gather(
                *(mcp_server._handle_tool_call("vault_status", {}) for _ in range(12))
            )

        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
        monkeypatch.setattr(mcp_server, "_vault_status", blocked)
        try:
            results = asyncio.run(exercise())
        finally:
            release.set()
            all_done.wait(0.5)

        assert peak <= mcp_server.MCP_WORKER_SLOTS
        assert all(json.loads(item)["data"] == {"error": "operation_timeout"} for item in results)

    def test_timed_out_blocked_worker_does_not_keep_process_alive(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = (
            "import asyncio,json,sys,threading; "
            f"sys.path.insert(0, {str(scripts)!r}); "
            "import mcp_server; "
            "mcp_server.MCP_OPERATION_SECONDS=0.05; "
            "mcp_server._vault_status=lambda *,deadline: threading.Event().wait(); "
            "result=asyncio.run(mcp_server._handle_tool_call('vault_status',{})); "
            "assert json.loads(result)['data']=={'error':'operation_timeout'}; "
            "print('TIMEOUT_RETURNED', flush=True)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "TIMEOUT_RETURNED"
        shutdown_started = time.perf_counter()
        stdout, stderr = process.communicate(timeout=1.0)

        assert process.returncode == 0, stderr
        assert stdout == ""
        assert time.perf_counter() - shutdown_started < 0.5

    def test_thread_start_failure_releases_reserved_worker_slot(self, monkeypatch):
        import threading

        import mcp_server

        class BrokenThread:
            def __init__(self, **kwargs):
                del kwargs

            def start(self):
                raise RuntimeError("cannot start thread")

        workers = set()
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", workers)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
        monkeypatch.setattr(mcp_server.threading, "Thread", BrokenThread)

        for _ in range(mcp_server.MCP_WORKER_SLOTS + 1):
            with pytest.raises(RuntimeError, match="cannot start"):
                asyncio.run(
                    mcp_server._run_bounded(
                        lambda: None, deadline=time.monotonic() + 1
                    )
                )

        assert workers == set()

    def test_final_envelope_sanitizes_unknown_name_and_helper_diagnostics(self):
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        unknown = json.loads(
            mcp_server._execute_tool_call(sensitive, {}, time.monotonic() + 1)
        )
        helper = mcp_server._build_operation_envelope(
            {"nested": {"error": f"failed at {sensitive}"}},
            {"warnings": [f"warning from {sensitive}"]},
        )

        for envelope in (unknown, helper):
            encoded = json.dumps(envelope)
            assert sensitive not in encoded
            assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
            assert r"C:\private\vault" not in encoded
        assert "Unknown tool:" in unknown["data"]["error"]
        assert "failed at" in helper["data"]["nested"]["error"]

    @pytest.mark.parametrize("action", ["queue-cancel", "queue-redrive"])
    def test_queue_mutations_receive_exact_doctor_deadline(self, monkeypatch, action):
        import mcp_server
        import memory_queue

        received = []

        class Queue:
            def __init__(self, root):
                del root

            def cancel(self, target_id, *, deadline, cancelled):
                received.append((target_id, deadline, cancelled()))
                return True

            def redrive(self, target_id, *, deadline, cancelled):
                received.append((target_id, deadline, cancelled()))
                return "replacement"

        monkeypatch.setattr(memory_queue, "MemoryQueue", Queue)
        deadline = time.monotonic() + 5

        result = mcp_server._doctor(
            action=action, target_id="task", repair=True, deadline=deadline
        )

        assert result["overall_status"] == "ok"
        assert received == [("task", deadline, False)]

    def test_transaction_undo_and_apply_receive_exact_doctor_deadline(self, monkeypatch):
        import markdown_transaction
        import mcp_server

        received = []

        class Record:
            id = "undo"
            state = "prepared"
            error_code = None

        class AppliedRecord:
            id = "undo"
            state = "committed"
            error_code = None

        class Coordinator:
            def __init__(self, *args):
                del args

            def undo(self, target_id, *, deadline, cancelled):
                received.append(("undo", target_id, deadline, cancelled()))
                return Record()

            def apply(self, transaction_id, *, deadline, cancelled):
                received.append(("apply", transaction_id, deadline, cancelled()))
                return AppliedRecord()

        monkeypatch.setattr(markdown_transaction, "MarkdownCoordinator", Coordinator)
        deadline = time.monotonic() + 5

        result = mcp_server._doctor(
            action="transaction-undo",
            target_id="original",
            repair=True,
            deadline=deadline,
        )

        assert result["overall_status"] == "ok"
        assert received == [
            ("undo", "original", deadline, False),
            ("apply", "undo", deadline, False),
        ]

    @pytest.mark.parametrize("action", ["queue-cancel", "queue-redrive"])
    def test_timed_out_queue_mutation_rolls_back_delayed_sql_commit(
        self, tmp_path, monkeypatch, action
    ):
        import threading
        from contextlib import contextmanager

        import mcp_server
        import memory_queue
        import memory_state

        state_root = tmp_path / "state"
        vault = tmp_path / "vault"
        vault.mkdir()
        queue = memory_queue.MemoryQueue(state_root)
        task_id = queue.enqueue("query", 1, {})
        if action == "queue-redrive":
            lease = queue.claim("worker")
            assert lease is not None
            queue.fail(lease, memory_queue.QueueFailure("invalid_input", permanent=True))

        commit_reached = threading.Event()
        release_commit = threading.Event()

        @contextmanager
        def delayed_begin(connection, *, before_commit=None):
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                commit_reached.set()
                assert release_commit.wait(60.0)
                if before_commit is not None:
                    before_commit()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        monkeypatch.setattr(memory_queue, "begin_immediate", delayed_begin)
        monkeypatch.setattr(memory_queue, "MemoryQueue", lambda _root: queue)
        monkeypatch.setattr(memory_state, "ROOT", vault)
        monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())

        try:
            text = asyncio.run(
                mcp_server._handle_tool_call(
                    "doctor",
                    {"action": action, "target_id": task_id, "repair": True},
                )
            )
            assert commit_reached.is_set()
            assert json.loads(text)["data"] == {"error": "operation_timeout"}
            assert queue.get(task_id).state == (
                "ready" if action == "queue-cancel" else "dead"
            )
        finally:
            release_commit.set()

        deadline = time.monotonic() + 30
        while mcp_server._MCP_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not mcp_server._MCP_WORKERS
        assert queue.get(task_id).state == (
            "ready" if action == "queue-cancel" else "dead"
        )
        assert [task.redrive_of for task in queue.list_tasks() if task.redrive_of] == []

    def test_timed_out_log_decision_never_appends_after_response(
        self, tmp_path, monkeypatch
    ):
        import threading
        from contextlib import contextmanager

        import markdown_transaction
        import mcp_server
        import memory_state

        vault = tmp_path / "vault"
        state_root = tmp_path / "state"
        daily = vault / "knowledge/daily" / f"{datetime.now():%Y-%m-%d}.md"
        daily.parent.mkdir(parents=True)
        daily.write_bytes(b"# existing\n")
        commit_reached = threading.Event()
        release_commit = threading.Event()

        @contextmanager
        def delayed_begin(connection, *, before_commit=None):
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                commit_reached.set()
                assert release_commit.wait(60.0)
                if before_commit is not None:
                    before_commit()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
        monkeypatch.setattr(memory_state, "ROOT", vault)
        monkeypatch.setattr(markdown_transaction, "begin_immediate", delayed_begin)
        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())

        async def exercise():
            task = asyncio.create_task(
                mcp_server._handle_tool_call(
                    "log_decision", {"summary": "must not append late"}
                )
            )
            while not commit_reached.is_set() and not task.done():
                await asyncio.sleep(0.01)
            return commit_reached.is_set(), await task

        try:
            reached_commit, text = asyncio.run(exercise())
            assert reached_commit is True
            assert json.loads(text)["data"] == {"error": "operation_timeout"}
            assert daily.read_bytes() == b"# existing\n"
        finally:
            release_commit.set()

        deadline = time.monotonic() + 30
        while mcp_server._MCP_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not mcp_server._MCP_WORKERS
        assert daily.read_bytes() == b"# existing\n"

    def test_late_worker_exception_is_observable_without_secret_text(
        self, monkeypatch, capsys
    ):
        import threading

        import mcp_server

        finished = threading.Event()
        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())

        def fail_late():
            time.sleep(0.3)
            try:
                raise RuntimeError(sensitive)
            finally:
                finished.set()

        with pytest.raises(TimeoutError):
            asyncio.run(
                mcp_server._run_bounded(
                    fail_late, deadline=time.monotonic() + 0.1
                )
            )
        assert finished.wait(2.0)
        deadline = time.monotonic() + 1.0
        while mcp_server._MCP_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)

        stderr = capsys.readouterr().err
        assert "RuntimeError" in stderr
        assert sensitive not in stderr
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in stderr

    def test_doctor_receives_exact_handler_deadline_after_dispatch_delay(
        self, monkeypatch
    ):
        import doctor
        import mcp_server

        now = [100.0]
        captured = []
        real_validate = mcp_server._validate_tool_arguments

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        def delayed_validate(name, arguments):
            now[0] = 104.0
            return real_validate(name, arguments)

        def run_doctor(**kwargs):
            captured.append(kwargs)
            return {
                "overall_status": "ok",
                "checks": [],
                "counts": {"ok": 0, "degraded": 0, "error": 0},
                "run_deletion": {"blockers": []},
            }

        monkeypatch.setattr(mcp_server, "time", Clock)
        monkeypatch.setattr(mcp_server, "_validate_tool_arguments", delayed_validate)
        monkeypatch.setattr(doctor, "run_doctor", run_doctor)

        envelope = json.loads(self._run("doctor", {"action": "status"}))

        assert envelope["data"]["overall_status"] == "ok"
        assert captured[0]["deadline"] == 100.0 + mcp_server.MCP_OPERATION_SECONDS
        assert "time_budget_seconds" not in captured[0]

    def test_recall_exposes_validated_planner_trace_and_component_generation(self, monkeypatch):
        import mcp_server

        row = {
            "path": "knowledge/notes/auth.md",
            "requested_mode": "HYBRID",
            "effective_mode": "BASE",
            "signals_used": ["lexical"],
            "fallback_reason": "dense_unavailable",
            "generation": "gen-17",
            "reranker_applied": False,
            "reranker_model_id": None,
            "reranker_model_revision": None,
            "reranker_depth": None,
            "reranker_duration_ms": None,
            "reranker_fallback_reason": None,
        }
        monkeypatch.setattr(mcp_server, "_search_vault", lambda *args, **kwargs: [row])
        monkeypatch.setattr(mcp_server, "_meta", lambda: {})

        envelope = json.loads(self._run("recall", {"query": "auth"}))

        trace = envelope["data"]["retrieval_trace"]
        assert trace["requested_mode"] == "HYBRID"
        assert trace["effective_mode"] == "BASE"
        assert trace["corpus_generation"] == "gen-17"
        assert envelope["components"]["lexical"] == {
            "generation": "gen-17", "freshness": "fresh"
        }

    def test_empty_recall_does_not_claim_any_retrieval_signal(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "_search_vault", lambda *args, **kwargs: [])
        monkeypatch.setattr(mcp_server, "_meta", lambda: {})

        envelope = json.loads(self._run("recall", {"query": "missing"}))

        assert envelope["data"]["retrieval_trace"]["signals_used"] == []
        assert envelope["data"]["retrieval_trace"]["fallback_reason"] == "trace_unavailable"
        assert all(
            detail["freshness"] != "fresh"
            for detail in envelope["components"].values()
        )

    def test_recall_rejects_trace_outside_the_closed_schema(self, monkeypatch):
        import mcp_server

        row = {
            "requested_mode": "HYBRID",
            "effective_mode": "invented",
            "signals_used": ["lexical"],
            "generation": "gen-17",
        }
        monkeypatch.setattr(mcp_server, "_search_vault", lambda *args, **kwargs: [row])

        envelope = json.loads(self._run("recall", {"query": "auth"}))

        assert "error" in envelope["data"]

    @pytest.mark.parametrize(
        ("mode", "args", "helper", "result_key"),
        [
            ("callers", {"symbol": "target"}, "find_callers", "callers"),
            ("callees", {"symbol": "target"}, "find_callees", "callees"),
            ("dependencies", {"symbol": "target"}, "find_dependencies", "dependencies"),
            ("path", {"symbol": "source", "target": "sink"}, "find_paths", "paths"),
            ("community", {}, "detect_communities", "communities"),
        ],
    )
    def test_get_architecture_dispatches_graph_modes(
        self, tmp_path, monkeypatch, mode, args, helper, result_key
    ):
        import code_graph

        calls = []
        monkeypatch.setattr(
            code_graph,
            helper,
            lambda *values, **options: calls.append((values, options))
            or {result_key: [], "source_generation": None, "graph_complete": False,
                "unresolved_count": 0, "fallback": True},
        )

        data = self._data(
            "get_architecture", {"directory": str(tmp_path), "mode": mode, **args}
        )

        assert data["mode"] == mode
        assert result_key in data["architecture"]
        assert calls
        assert calls[0][1]["with_report"] is True

    def test_get_architecture_symbol_mode_combines_existing_graph_queries(
        self, tmp_path, monkeypatch
    ):
        import code_graph

        report = {
            "source_generation": "graph-17",
            "graph_complete": True,
            "unresolved_count": 0,
            "fallback": False,
        }
        monkeypatch.setattr(
            code_graph, "find_callers", lambda *args, **kwargs: {"callers": [1], **report}
        )
        monkeypatch.setattr(
            code_graph, "find_callees", lambda *args, **kwargs: {"callees": [2], **report}
        )
        monkeypatch.setattr(
            code_graph,
            "find_dependencies",
            lambda *args, **kwargs: {"dependencies": [3], **report},
        )

        envelope = json.loads(
            self._run(
                "get_architecture",
                {"directory": str(tmp_path), "mode": "symbol", "symbol": "target"},
            )
        )

        assert envelope["data"]["architecture"] == {
            "symbol": "target",
            "callers": [1],
            "callees": [2],
            "dependencies": [3],
            **report,
        }
        assert envelope["components"]["graph"] == {
            "generation": "graph-17",
            "freshness": "fresh",
        }

    def test_external_repository_never_receives_active_repository_graph(
        self, tmp_path, monkeypatch
    ):
        from generation_catalog import GenerationCatalog
        from repository_scope import resolve_repository_scope

        from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

        repository_a = tmp_path / "owner-a" / "project"
        repository_b = tmp_path / "owner-b" / "project"
        repository_a.mkdir(parents=True)
        repository_b.mkdir(parents=True)
        (repository_b / "b.py").write_text(
            "def b_only_symbol():\n    pass\n", encoding="utf-8"
        )
        records = _rich_graph_records()
        for node in records["nodes"]:
            node["metadata"]["name"] = "a_only_symbol"
        state = tmp_path / "shared-state"
        catalog = GenerationCatalog(state)
        _publish(
            catalog,
            "repo-a",
            graph_records=records,
            repository_scope=resolve_repository_scope(repository_a).as_dict(),
        )
        catalog.register("repo-a")
        assert catalog.activate("repo-a", expected_active=None)
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))

        dead = json.loads(
            self._run("find_dead_code", {"directory": str(repository_b)})
        )
        architecture = json.loads(
            self._run("get_architecture", {"directory": str(repository_b)})
        )

        for envelope in (dead, architecture):
            encoded = json.dumps(envelope)
            assert envelope["data"]["source_generation"] is None
            assert envelope["data"]["fallback"] is True
            assert "a_only_symbol" not in encoded
            assert str(repository_a) not in encoded
            assert str(repository_b / "app.py") not in encoded

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

    def test_compile_returns_json(self, monkeypatch):
        import compile_memory

        monkeypatch.setattr(compile_memory, "run_pending_compile", lambda **kwargs: 0)
        data = self._data("compile", {})
        assert data == {"status": "completed", "returncode": 0}

    @pytest.mark.parametrize("tool_name", VALID_TOOL_CALLS)
    def test_every_helper_exception_returns_degraded_envelope(self, monkeypatch, tool_name):
        import mcp_server

        def fail(*args, **kwargs):
            raise RuntimeError(f"{tool_name} failed")

        monkeypatch.setattr(mcp_server, TOOL_HELPERS[tool_name], fail)

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert "error" in envelope["data"]
        assert f"{tool_name} failed" not in json.dumps(envelope)
        assert len(envelope["data"]["error"]) <= 256
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

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("recall", {"query": "x" * 8193}),
            ("get_decisions", {"query": "x" * 8193}),
            ("read_page", {"slug": "x" * 256}),
            ("get_context", {"slugs": []}),
            ("get_context", {"slugs": [f"page-{index}" for index in range(21)]}),
            ("get_context", {"slugs": ["same", "same"]}),
            ("get_context", {"slugs": ["page"], "include": [str(index) for index in range(11)]}),
            ("get_context", {"slugs": ["page"], "include": ["x" * 65]}),
        ],
    )
    def test_retrieval_bounds_are_rejected_before_helper_dispatch(
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
        monkeypatch.setattr(mcp_server, "_doctor", lambda **kwargs: {"overall_status": "ok"})
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
    def test_legacy_index_age_does_not_claim_component_freshness(
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

        assert envelope["index_timestamp"] is None
        if tool_name != "recall":
            assert envelope["freshness"] == "unknown"
        assert not any(
            "index freshness" in warning.lower() for warning in envelope["warnings"]
        )

    def test_get_context_accepts_unrecognized_include_strings(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_get_context",
            lambda slugs, include: {"include": include},
        )

        envelope = json.loads(
            self._run(
                "get_context", {"slugs": ["missing"], "include": ["future-option"]}
            )
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
        monkeypatch.setattr("code_graph.find_dead_code", lambda directory, **options: expected)

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
        monkeypatch.setattr("code_graph.get_architecture", lambda directory, **options: expected)

        envelope = json.loads(self._run("get_architecture", {"directory": str(tmp_path)}))
        data = envelope["data"]
        assert data["architecture"] == expected
        assert data["directory"] == str(tmp_path.resolve())
        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_code_tool_envelope_uses_generation_report(self, tmp_path, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_find_dead_code",
            lambda directory, **options: {
                "directory": directory,
                "candidates": [],
                "source_generation": "gen-25",
                "graph_complete": True,
                "unresolved_count": 0,
                "fallback": False,
            },
        )

        envelope = json.loads(
            asyncio.run(
                mcp_server._handle_tool_call(
                    "find_dead_code", {"directory": str(tmp_path)}
                )
            )
        )

        assert envelope["fallback"] is False
        assert envelope["partial"] is False
        assert envelope["coverage"] >= 0.9
        assert envelope["data"]["source_generation"] == "gen-25"

    def test_doctor_returns_uniform_conservative_envelope(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_doctor",
            lambda **kwargs: {
                "overall_status": "degraded",
                "checks": [{"id": "index", "status": "degraded", "message": "stale"}],
            },
        )

        envelope = json.loads(self._run("doctor", {"action": "status"}))

        assert set(envelope) == ENVELOPE_FIELDS
        assert envelope["data"]["overall_status"] == "degraded"
        assert envelope["partial"] is True
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_doctor_operator_failure_is_protocol_error_with_error_quality(
        self, monkeypatch
    ):
        import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_doctor",
            lambda **kwargs: {
                "action": kwargs["action"],
                "overall_status": "error",
                "ids": [],
                "counts": {"items": 0},
                "states": ["error"],
                "codes": ["operation_failed"],
            },
        )

        envelope = json.loads(
            self._run("doctor", {"action": "transaction-recover", "repair": True})
        )

        assert envelope["data"]["overall_status"] == "error"
        assert envelope["partial"] is True
        assert envelope["confidence"] < 0.5

    def test_transaction_recover_passes_limit_and_deadline_not_post_slice(
        self, monkeypatch
    ):
        received = []

        class Coordinator:
            def __init__(self, *args):
                pass

            def recover(self, **kwargs):
                received.append(kwargs)
                return []

        monkeypatch.setattr("markdown_transaction.MarkdownCoordinator", Coordinator)

        envelope = json.loads(
            self._run(
                "doctor",
                {"action": "transaction-recover", "repair": True, "limit": 7},
            )
        )

        assert envelope["data"]["overall_status"] == "ok"
        assert received[0]["max_transactions"] == 7
        assert received[0]["deadline"] > time.monotonic()

    @pytest.mark.parametrize(
        ("action", "behavior", "expected_code"),
        [
            ("queue-inspect", "missing", "unknown_task"),
            ("queue-cancel", "false", "unknown_or_terminal_task"),
            ("queue-redrive", "missing", "unknown_task"),
            ("queue-redrive", "invalid", "redrive_requires_dead"),
        ],
    )
    def test_rejected_queue_actions_use_error_quality_and_protocol_error(
        self, tmp_path, monkeypatch, action, behavior, expected_code
    ):
        import mcp_server
        import memory_queue
        import memory_state

        state_root = tmp_path / "state"
        run = state_root / "run"
        run.mkdir(parents=True)
        monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
        if action == "queue-inspect":
            database = run / "queue.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE tasks(id, state, error_code)")
        else:
            class Queue:
                def __init__(self, root):
                    pass

                def cancel(self, target_id, **kwargs):
                    del kwargs
                    return False

                def redrive(self, target_id, **kwargs):
                    del kwargs
                    if behavior == "missing":
                        raise KeyError(target_id)
                    raise memory_queue.QueueOperationError("redrive_requires_dead")

            monkeypatch.setattr(memory_queue, "MemoryQueue", Queue)

        data = mcp_server._doctor(
            action=action,
            target_id="missing-task",
            repair=action != "queue-inspect",
        )
        monkeypatch.setattr(mcp_server, "_doctor", lambda **kwargs: data)
        text = asyncio.run(
            mcp_server._handle_tool_call(
                "doctor",
                {
                    "action": action,
                    "target_id": "missing-task",
                    **({"repair": True} if action != "queue-inspect" else {}),
                },
            )
        )
        envelope = json.loads(text)

        assert data["overall_status"] == "error"
        assert data["codes"] == [expected_code]
        assert envelope["partial"] is True
        assert envelope["confidence"] < 0.5

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "TextContent", Model)
        monkeypatch.setattr(mcp_server, "CallToolResult", Model)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        assert mcp_server._format_tool_result(text).isError is True

    def test_queue_inspect_missing_database_is_protocol_error(self, tmp_path, monkeypatch):
        import mcp_server
        import memory_state

        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)

        data = mcp_server._doctor(action="queue-inspect", target_id="task")

        assert data["overall_status"] == "error"
        assert data["codes"] == ["queue_missing"]


class TestResources:
    def test_resource_inventory_remains_exactly_two(self, monkeypatch):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "MCP_RESOURCES_AVAILABLE", True)
        monkeypatch.setattr(mcp_server, "Resource", Model)

        resources = mcp_server._build_resource_definitions()

        assert len(resources) == 2
        assert {resource.uri for resource in resources} == {
            mcp_server.HEALTH_RESOURCE_URI,
            mcp_server.CONTEXT_RESOURCE_URI,
        }

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

    def test_stale_legacy_index_does_not_degrade_context_resource_quality(
        self, monkeypatch, tmp_path
    ):
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

        assert envelope["freshness"] == "unknown"
        assert envelope["partial"] is False
        assert envelope["coverage"] > 0.6

    def test_unknown_resource_returns_enveloped_error(self):
        from mcp_server import _handle_resource_read

        envelope = json.loads(_handle_resource_read("llm-wiki://unknown"))

        assert set(envelope) == ENVELOPE_FIELDS
        assert "error" in envelope["data"]

    def test_resource_exception_text_is_safe_and_bounded(self, monkeypatch):
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\prompt.txt"
        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda: (_ for _ in ()).throw(RuntimeError(sensitive * 100)),
        )

        envelope = json.loads(mcp_server._handle_resource_read("llm-wiki://health"))

        assert sensitive not in json.dumps(envelope)
        assert len(envelope["data"]["error"]) <= 256

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

    @pytest.mark.parametrize(
        "uri", ["llm-wiki://health", "llm-wiki://context"]
    )
    def test_registered_resources_share_one_deadline_and_run_off_loop(
        self, monkeypatch, uri
    ):
        import mcp_server

        seen = []
        release = threading.Event()
        finished = threading.Event()
        drained = threading.Event()
        workers = DrainAwareSet(drained)

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

        async def exercise(callback):
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def blocked(*, deadline):
                seen.append(deadline)
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait(60.0)
                finally:
                    finished.set()
                return {"last_compile": "never", "last_compile_status": "unknown"}

            monkeypatch.setattr(mcp_server, "_vault_status", blocked)
            task = asyncio.create_task(callback(uri))
            try:
                await asyncio.wait_for(started.wait(), timeout=0.5)
                await asyncio.sleep(0.01)
                loop_progressed = not finished.is_set()
                return loop_progressed, await task
            finally:
                release.set()
                assert finished.wait(1.0)
                assert drained.wait(1.0)
                with mcp_server._MCP_WORKERS_LOCK:
                    assert not workers

        server = FakeServer()
        monkeypatch.setattr(mcp_server, "MCP_RESOURCES_AVAILABLE", True)
        monkeypatch.setattr(mcp_server, "Resource", Model)
        monkeypatch.setattr(mcp_server, "TextResourceContents", Model)
        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS", workers)
        monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
        monkeypatch.setattr(mcp_server, "_wiki_overview", lambda **kwargs: {"ok": True})
        assert mcp_server._register_resources(server) is True
        loop_progressed, contents = asyncio.run(exercise(server.callbacks["read"]))

        envelope = json.loads(contents[0].text)
        assert loop_progressed is True
        assert len(seen) == 1
        assert envelope["data"] == {"error": "operation_timeout"}

        status = {"last_compile": "fresh", "last_compile_status": "ok"}
        monkeypatch.setattr(
            mcp_server,
            "_vault_status",
            lambda *, deadline: status,
        )
        sentinel_contents = asyncio.run(server.callbacks["read"](uri))
        sentinel = json.loads(sentinel_contents[0].text)
        expected = status
        if uri == mcp_server.CONTEXT_RESOURCE_URI:
            expected = {"overview": {"ok": True}, "status": status}
        assert sentinel["data"] == expected
        with mcp_server._MCP_WORKERS_LOCK:
            assert not workers


class TestCallbackCompatibility:
    def test_registered_callback_formats_large_result_off_event_loop(self, monkeypatch):
        import threading

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
                return lambda callback: setattr(self, "callback", callback) or callback

        server = FakeServer()
        monkeypatch.setattr(mcp_server, "TextContent", Model)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
        mcp_server._register_tools(server, [])
        payload = mcp_server._timeout_envelope_text().replace(
            '"operation_timeout"', json.dumps("x" * (mcp_server.MAX_MCP_PAGE_BYTES - 1024)), 1
        )
        real_format = mcp_server._format_tool_result
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_format(text):
            started.set()
            try:
                release.wait(60.0)
                return real_format(text)
            finally:
                finished.set()

        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 1.0)
        monkeypatch.setattr(mcp_server, "_execute_tool_call", lambda *args: payload)
        monkeypatch.setattr(mcp_server, "_format_tool_result", slow_format)

        async def exercise():
            task = asyncio.create_task(server.callback("read_page", {"slug": "page"}))
            await asyncio.sleep(0.02)
            heartbeat = started.is_set() and not finished.is_set()
            release.set()
            return heartbeat, await task

        heartbeat, result = asyncio.run(exercise())

        assert heartbeat is True
        assert len(result[0].text) >= mcp_server.MAX_MCP_PAGE_BYTES - 2048

    def test_registered_callback_formatter_deadline_returns_cached_timeout(
        self, monkeypatch
    ):
        import threading

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
                return lambda callback: setattr(self, "callback", callback) or callback

        server = FakeServer()
        monkeypatch.setattr(mcp_server, "TextContent", Model)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
        mcp_server._register_tools(server, [])
        release = threading.Event()
        real_format = mcp_server._format_tool_result

        def blocked_format(text):
            release.wait(0.5)
            return real_format(text)

        monkeypatch.setattr(mcp_server, "MCP_OPERATION_SECONDS", 0.001)
        monkeypatch.setattr(
            mcp_server,
            "_execute_tool_call",
            lambda *args: mcp_server._timeout_envelope_text(),
        )
        monkeypatch.setattr(mcp_server, "_format_tool_result", blocked_format)
        started = time.perf_counter()
        try:
            result = asyncio.run(server.callback("read_page", {"slug": "page"}))
        finally:
            release.set()

        assert time.perf_counter() - started < 0.2
        assert json.loads(result[0].text)["data"] == {"error": "operation_timeout"}

    @pytest.mark.parametrize(
        ("arguments", "report", "expected_error"),
        [
            (
                {"action": "transaction-recover", "repair": True},
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
                {"action": "status"},
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
                True,
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
        monkeypatch.setattr(mcp_server, "_doctor", lambda **kwargs: report)
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

    def test_call_tool_result_marks_operator_overall_error(self, monkeypatch):
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
        text = json.dumps(
            {"data": {"overall_status": "error", "codes": ["operation_failed"]}}
        )

        assert mcp_server._format_tool_result(text).isError is True

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

    @pytest.mark.parametrize(
        ("status", "expected_error"),
        [
            ("error", True),
            ("timeout", True),
            ("unsupported", False),
            ("not_ready", False),
            ("partial", False),
            ("stale", False),
            ("ok", False),
        ],
    )
    def test_call_tool_result_uses_precise_navigation_failure_semantics(
        self,
        monkeypatch,
        status,
        expected_error,
    ):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(mcp_server, "TextContent", Model)
        monkeypatch.setattr(mcp_server, "CallToolResult", Model)
        monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        data = mcp_server._normalized_navigation_failure(
            directory=None,
            mode="definition",
            status=status,
            warning="navigation_warning",
            offset=0,
            limit=10,
        )

        result = mcp_server._format_tool_result(json.dumps({"data": data}))

        assert result.isError is expected_error

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


@pytest.mark.parametrize(
    "mode",
    ["definition", "references", "implementations", "type", "diagnostics"],
)
def test_precise_modes_require_exact_position_triple(mode: str) -> None:
    import mcp_server

    error = mcp_server._validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode, "path": "pkg/api.py"},
    )
    assert error is not None
    assert "line" in error or "character" in error


def test_precise_modes_reject_symbol_substitute() -> None:
    import mcp_server

    error = mcp_server._validate_tool_arguments(
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

    error = mcp_server._validate_tool_arguments(
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

    error = mcp_server._validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode, "path": "pkg/api.py", "line": 1},
    )
    assert error is not None
    assert "character" in error or "together" in error


@pytest.mark.parametrize("mode", ["callers", "callees"])
def test_structural_calls_keep_symbol_behavior(mode: str) -> None:
    import mcp_server

    error = mcp_server._validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode, "symbol": "f"},
    )
    assert error is None


def test_navigation_deadline_is_60s_for_precise_modes() -> None:
    import mcp_server

    assert (
        mcp_server._tool_operation_seconds(
            "get_architecture",
            {"mode": "definition", "path": "pkg/api.py", "line": 1, "character": 0},
        )
        == mcp_server.MCP_LSP_STARTUP_SECONDS
    )
    assert (
        mcp_server._tool_operation_seconds(
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
        mcp_server._tool_operation_seconds(
            "get_architecture", {"mode": "summary"}
        )
        == mcp_server.MCP_OPERATION_SECONDS
    )
    assert (
        mcp_server._tool_operation_seconds(
            "get_architecture", {"mode": "callers", "symbol": "f"}
        )
        == mcp_server.MCP_OPERATION_SECONDS
    )
    assert (
        mcp_server._tool_operation_seconds("recall", {"query": "x"})
        == mcp_server.MCP_OPERATION_SECONDS
    )


def test_precise_request_classification_is_exact() -> None:
    import mcp_server

    assert mcp_server._is_precise_architecture_request(
        {"mode": "definition", "path": "p.py", "line": 1, "character": 0}
    )
    assert mcp_server._is_precise_architecture_request(
        {"mode": "callers", "path": "p.py", "line": 1, "character": 0}
    )
    assert not mcp_server._is_precise_architecture_request(
        {"mode": "callers", "symbol": "f"}
    )
    assert not mcp_server._is_precise_architecture_request({"mode": "summary"})
    assert not mcp_server._is_precise_architecture_request(
        {"mode": "callers", "path": "p.py", "line": 1}
    )


def test_precise_architecture_rejects_non_checkout_directory(
    monkeypatch,
) -> None:
    import mcp_server

    def _fail_get(*args, **kwargs):
        raise AssertionError("session manager must not be called for bad directory")

    monkeypatch.setattr(mcp_server, "_navigation_session_manager", _fail_get)
    data = mcp_server._get_precise_architecture(
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

    monkeypatch.setattr(mcp_server, "_get_precise_architecture", _fail_precise)
    monkeypatch.setattr("code_graph.find_callers", lambda *a, **k: {"callers": []})
    resolved = str(Path(__file__).resolve().parent.parent)
    data = mcp_server._get_architecture_mode(
        resolved, mode="callers", symbol="f", deadline=time.monotonic() + 5
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

    error = mcp_server._validate_tool_arguments("get_architecture", arguments)

    assert error is not None
    assert invalid_field in error


@pytest.mark.parametrize(
    "mode",
    ["definition", "references", "implementations", "type", "diagnostics"],
)
def test_exact_modes_list_every_missing_position_field(mode: str) -> None:
    import mcp_server

    error = mcp_server._validate_tool_arguments(
        "get_architecture",
        {"directory": "C:/repo", "mode": mode},
    )

    assert error is not None
    assert all(field in error for field in ("path", "line", "character"))


@pytest.mark.parametrize("mode", ["callers", "callees"])
def test_positioned_calls_reject_symbol_even_with_complete_position(mode: str) -> None:
    import mcp_server

    error = mcp_server._validate_tool_arguments(
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

    assert mcp_server._validate_tool_arguments("get_architecture", arguments) is None


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

    assert mcp_server._validate_tool_arguments("get_architecture", arguments)


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
    monkeypatch.setattr(mcp_server, "time", Clock)
    monkeypatch.setattr(mcp_server, "_run_bounded", bounded)
    monkeypatch.setattr(mcp_server, "MCP_CALL_TOOL_RESULT_AVAILABLE", False)
    monkeypatch.setattr(mcp_server, "MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
    monkeypatch.setattr(
        mcp_server,
        "TextContent",
        lambda **kwargs: type("Text", (), kwargs)(),
    )
    mcp_server._register_tools(server, [])

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

    assert mcp_server._same_filesystem_path(tmp_path, variant) is True


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
    monkeypatch.setattr(mcp_server, "_operation_cancelled", lambda: cancelled)
    monkeypatch.setattr(
        lsp_security,
        "resolve_repository_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(
        mcp_server,
        "_navigation_session_manager",
        lambda *_args, **_kwargs: pytest.fail("manager must follow source containment"),
    )
    deadline = time.monotonic() + 5

    data = mcp_server._get_precise_architecture(
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

    monkeypatch.setattr(mcp_server, "_navigation_session_manager", manager)

    data = mcp_server._get_precise_architecture(
        str(tmp_path),
        mode="definition",
        path=path,
        line=1,
        character=0,
        deadline=time.monotonic() + 5,
    )

    assert manager_calls == 0
    assert data["status"] == "error"
    assert "error" not in data


def test_navigation_manager_singleton_lock_honors_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import mcp_server

    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", None)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 7, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", lock)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="manager lock"):
        mcp_server._navigation_session_manager(
            started + 0.02,
            7,
            lambda: False,
        )

    assert time.monotonic() - started < 0.2
    lock.release()


def test_navigation_manager_existing_lookup_still_honors_lock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import mcp_server

    lock = threading.Lock()
    lock.acquire()
    manager = object()
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", manager)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 7, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", lock)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="manager lock"):
        mcp_server._navigation_session_manager(
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
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", manager)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 11, raising=False)
    deadline = time.monotonic() + 5

    mcp_server._close_navigation_session_manager(deadline)

    assert closed == [deadline]
    assert mcp_server._NAVIGATION_MANAGER is None
    assert mcp_server._NAVIGATION_MANAGER_CLOSING is None
    assert mcp_server._NAVIGATION_MANAGER_EPOCH == 12


def test_close_navigation_manager_advances_epoch_without_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import mcp_server

    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", None)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 23, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", threading.Lock())

    mcp_server._close_navigation_session_manager(time.monotonic() + 5)

    assert mcp_server._NAVIGATION_MANAGER_EPOCH == 24
    assert mcp_server._NAVIGATION_MANAGER is None
    assert mcp_server._NAVIGATION_MANAGER_CLOSING is None


def test_navigation_manager_rejects_cancelled_and_stale_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import mcp_server
    import pyright_session

    def forbidden(*_args, **_kwargs):
        raise AssertionError("rejected request constructed a manager")

    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", None)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 5, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", threading.Lock())
    monkeypatch.setattr(pyright_session, "PyrightSessionManager", forbidden)
    deadline = time.monotonic() + 5

    with pytest.raises(TimeoutError, match="cancelled"):
        mcp_server._navigation_session_manager(deadline, 5, lambda: True)
    with pytest.raises(TimeoutError, match="lifecycle"):
        mcp_server._navigation_session_manager(deadline, 4, lambda: False)

    assert mcp_server._NAVIGATION_MANAGER is None


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
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", manager)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 31, raising=False)

    with pytest.raises(TimeoutError, match="close failed"):
        mcp_server._close_navigation_session_manager(time.monotonic() + 5)

    assert mcp_server._NAVIGATION_MANAGER is None
    assert mcp_server._NAVIGATION_MANAGER_CLOSING is manager
    with pytest.raises(TimeoutError, match="closing"):
        mcp_server._navigation_session_manager(
            time.monotonic() + 0.02,
            mcp_server._NAVIGATION_MANAGER_EPOCH,
            lambda: False,
        )

    mcp_server._close_navigation_session_manager(time.monotonic() + 5)

    assert attempts == 2
    assert mcp_server._NAVIGATION_MANAGER is None
    assert mcp_server._NAVIGATION_MANAGER_CLOSING is None


def test_navigation_manager_constructor_crossing_deadline_is_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import mcp_server
    import pyright_session

    now = [100.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return now[0]

    class Manager:
        def __init__(self, *, state_root: Path) -> None:
            del state_root
            now[0] = 101.0

    monkeypatch.setattr(mcp_server, "time", Clock)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", None)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 2, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", threading.Lock())
    monkeypatch.setattr(pyright_session, "PyrightSessionManager", Manager)

    with pytest.raises(TimeoutError):
        mcp_server._navigation_session_manager(100.5, 2, lambda: False)

    assert mcp_server._NAVIGATION_MANAGER is None


def test_successful_close_crossing_deadline_still_resets_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

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
    monkeypatch.setattr(mcp_server, "time", Clock)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", manager)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 41, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", threading.Lock())

    with pytest.raises(TimeoutError):
        mcp_server._close_navigation_session_manager(100.5)

    assert mcp_server._NAVIGATION_MANAGER is None
    assert mcp_server._NAVIGATION_MANAGER_CLOSING is None
    assert mcp_server._NAVIGATION_MANAGER_EPOCH == 42


def test_timed_out_worker_cannot_recreate_manager_after_final_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import code_navigation
    import code_navigation_renderer
    import lsp_security
    import mcp_server
    import pyright_session
    import repository_scope

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

    class Manager:
        def __init__(self, *, state_root: Path) -> None:
            del state_root
            constructed.append(self)

        def get(self, _scope, *, deadline: float):
            assert deadline > time.monotonic()
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
            assert release.wait(2)
        return tmp_path, None

    def request():
        try:
            stale_results.append(
                mcp_server._get_precise_architecture(
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

    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER", None)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_CLOSING", None, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 101, raising=False)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_LOCK", threading.Lock())
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
    monkeypatch.setattr(mcp_server, "_validated_code_directory", delayed_directory)
    monkeypatch.setattr(mcp_server, "_operation_cancelled", lambda: lambda: False)
    monkeypatch.setattr(mcp_server, "_same_filesystem_path", lambda *_args, **_kwargs: True)
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
    monkeypatch.setattr(pyright_session, "PyrightSessionManager", Manager)
    monkeypatch.setattr(code_navigation, "CodeNavigation", Navigation)
    monkeypatch.setattr(
        code_navigation_renderer,
        "render_navigation",
        lambda _result: {"status": "ok", "warnings": (), "provenance": []},
    )

    async def time_out_request():
        with pytest.raises(TimeoutError, match="deadline"):
            await mcp_server._run_bounded(
                request,
                deadline=time.monotonic() + 0.05,
            )

    try:
        asyncio.run(time_out_request())
        assert entered.is_set()
        mcp_server._close_navigation_session_manager(time.monotonic() + 5)
        assert mcp_server._NAVIGATION_MANAGER_EPOCH == 102
        release.set()
        assert completed.wait(2)
        assert stale_results[0]["status"] == "timeout"
        assert constructed == []
        assert mcp_server._NAVIGATION_MANAGER is None

        fresh = mcp_server._get_precise_architecture(
            str(tmp_path),
            mode="definition",
            path="api.py",
            line=1,
            character=4,
            deadline=time.monotonic() + 5,
        )

        assert fresh["status"] == "ok"
        assert len(constructed) == 1
        assert mcp_server._NAVIGATION_MANAGER is constructed[0]
    finally:
        release.set()
        completed.wait(2)
        if mcp_server._NAVIGATION_MANAGER is not None:
            mcp_server._close_navigation_session_manager(time.monotonic() + 5)


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

    monkeypatch.setattr(mcp_server, "MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_server, "Server", FakeServer)
    monkeypatch.setattr(mcp_server, "stdio_server", Stdio)
    monkeypatch.setattr(mcp_server, "_build_tool_definitions", lambda: [])
    monkeypatch.setattr(mcp_server, "_register_resources", lambda *_args: False)
    monkeypatch.setattr(mcp_server, "_register_tools", lambda *_args: None)
    monkeypatch.setattr(
        mcp_server,
        "_close_navigation_session_manager",
        lambda deadline: closed.append(deadline),
    )

    assert mcp_server.run_server() == 0
    assert len(closed) == 1
    assert closed[0] > time.monotonic()


@pytest.mark.parametrize(
    ("mode", "capability_name", "direction"),
    [
        ("definition", "DEFINITIONS", None),
        ("references", "REFERENCES", None),
        ("implementations", "IMPLEMENTATIONS", None),
        ("type", "TYPES", None),
        ("diagnostics", "DIAGNOSTICS", None),
        ("callers", "CALLS", "incoming"),
        ("callees", "CALLS", "outgoing"),
    ],
)
def test_every_precise_route_builds_one_request_and_one_renderer_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    capability_name: str,
    direction: str | None,
) -> None:
    import code_navigation
    import code_navigation_renderer
    import mcp_server

    source = tmp_path / "api.py"
    source.write_text("def api():\n    return 1\n", encoding="utf-8")
    identity = object()
    query_result = object()
    captures: dict[str, object] = {"requests": [], "renders": []}

    class Session:
        @property
        def identity(self):
            return identity

    class Manager:
        def get(self, scope, *, deadline):
            captures["manager"] = (scope, deadline)
            return Session()

    class Navigation:
        def __init__(self, scope, session, received_identity, **callbacks):
            captures["constructor"] = (
                scope,
                session,
                received_identity,
                callbacks,
            )

        def query(self, request, *, deadline):
            captures["requests"].append((request, deadline))
            return query_result

    def render(result):
        captures["renders"].append(result)
        return {"status": "ok", "warnings": (), "provenance": []}

    manager = Manager()
    manager_requests: list[tuple[float, int, object]] = []

    def manager_factory(deadline, expected_epoch, cancelled):
        manager_requests.append((deadline, expected_epoch, cancelled))
        return manager

    monkeypatch.setattr(code_navigation, "CodeNavigation", Navigation)
    monkeypatch.setattr(code_navigation_renderer, "render_navigation", render)
    monkeypatch.setattr(mcp_server, "_navigation_session_manager", manager_factory)
    monkeypatch.setattr(mcp_server, "_NAVIGATION_MANAGER_EPOCH", 73, raising=False)
    deadline = time.monotonic() + 5

    data = mcp_server._get_precise_architecture(
        str(tmp_path),
        mode=mode,
        path="api.py",
        line=1,
        character=4,
        offset=7,
        limit=8,
        deadline=deadline,
    )

    request, query_deadline = captures["requests"][0]
    constructor = captures["constructor"]
    assert request.capability.name == capability_name
    assert request.direction == direction
    assert (request.offset, request.limit) == (7, 8)
    assert query_deadline == deadline
    assert len(manager_requests) == 1
    assert manager_requests[0][:2] == (deadline, 73)
    assert callable(manager_requests[0][2])
    assert manager_requests[0][2]() is False
    assert captures["manager"][1] == deadline
    assert constructor[2] is identity
    assert set(constructor[3]) == {
        "structural_candidates",
        "symbol_resolver",
        "edge_verifier",
    }
    assert all(callable(callback) for callback in constructor[3].values())
    assert captures["renders"] == [query_result]
    assert len(captures["requests"]) == 1
    assert len(captures["renders"]) == 1
    assert data["mode"] == mode
    assert data["status"] == "ok"


def test_real_navigation_adapters_return_only_contained_exact_graph_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import lsp_security
    import mcp_server
    from code_intelligence import Capability
    from code_navigation import NavigationRequest
    from lsp_positions import SourceAnchor
    from repository_scope import resolve_repository_scope

    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    content = (
        b"def caller():\n"
        b"    callee()\n"
        b"\n"
        b"def callee():\n"
        b"    pass\n"
    )
    source.write_bytes(content)
    scope = resolve_repository_scope(tmp_path)
    caller_start = content.index(b"caller")
    call_start = content.index(b"callee")
    declaration_start = content.rindex(b"callee")
    source_hash = hashlib.sha256(content).hexdigest()
    reads: list[tuple[str, int, float | None]] = []
    original_read = lsp_security.read_repository_source_bytes

    class Graph:
        repository_scope = scope
        generation_id = "generation-17"

        def find_nodes(self, *, name, max_rows, deadline, **_options):
            assert max_rows == 10_000
            node_id = {"caller": "caller-node", "callee": "callee-node"}.get(name)
            return [] if node_id is None else [
                {
                    "node_id": node_id,
                    "kind": "function",
                    "identity_key": name,
                    "metadata": {"name": name},
                }
            ]

        def occurrences(self, node_id, *, max_rows, deadline):
            assert max_rows <= 10_000
            starts = {
                "caller-node": (caller_start, caller_start + len(b"caller"), 1),
                "callee-node": (
                    declaration_start,
                    declaration_start + len(b"callee"),
                    4,
                ),
            }
            if node_id not in starts:
                return []
            start, end, line = starts[node_id]
            return [
                {
                    "file": str(source),
                    "role": "definition",
                        "byte_start": start,
                        "byte_end": end,
                        "line_start": line,
                        "source_sha256": source_hash,
                    }
                ]

        def edges(self, *, edge_types, max_rows, deadline):
            assert edge_types == ("CALLS",)
            assert max_rows == 10_000
            return [
                {
                    "assertion_id": "call-edge",
                    "source_node_id": "caller-node",
                    "target_node_id": "callee-node",
                }
            ]

        def evidence_spans(self, *, assertion_id, max_rows, deadline):
            assert assertion_id == "call-edge"
            return [
                {
                    "relative_path": "src/app.py",
                    "byte_start": call_start,
                    "byte_end": call_start + len(b"callee"),
                    "line_start": 2,
                    "source_sha256": source_hash,
                    "span_sha256": hashlib.sha256(b"callee").hexdigest(),
                }
            ][:max_rows]

        def close(self):
            return None

    def stable_read(repository, relative_path, *, max_bytes, deadline=None):
        reads.append((relative_path, max_bytes, deadline))
        return original_read(
            repository,
            relative_path,
            max_bytes=max_bytes,
            deadline=deadline,
        )

    monkeypatch.setattr(
        code_graph, "_active_evidence_graph", lambda _root, **_options: Graph()
    )
    monkeypatch.setattr(lsp_security, "read_repository_source_bytes", stable_read)
    monkeypatch.setattr(
        code_graph,
        "index_directory",
        lambda *_args, **_kwargs: pytest.fail("adapter must never index"),
    )
    monkeypatch.setattr(
        code_graph,
        "detect_code_tools",
        lambda *_args, **_kwargs: pytest.fail("adapter must never write tool cache"),
    )
    deadline = time.monotonic() + 5
    definition_request = NavigationRequest(
        scope,
        Capability.DEFINITIONS,
        "src/app.py",
        2,
        6,
    )
    calls_request = NavigationRequest(
        scope,
        Capability.CALLS,
        "src/app.py",
        1,
        5,
        direction="outgoing",
    )

    definitions = mcp_server._navigation_structural_candidates(
        definition_request,
        deadline,
    )
    calls = mcp_server._navigation_structural_candidates(calls_request, deadline)
    resolved = mcp_server._navigation_symbol_resolver("callee", scope, deadline)
    verified = mcp_server._navigation_edge_verifier(
        SourceAnchor("src/app.py", 1, 4, caller_start),
        SourceAnchor("src/app.py", 4, 4, declaration_start),
        scope,
        deadline,
    )

    assert [(item.path, item.range.byte_start, item.range.byte_end) for item in definitions] == [
        ("src/app.py", declaration_start, declaration_start + len(b"callee"))
    ]
    assert [(item.path, item.range.byte_start, item.range.byte_end) for item in calls] == [
        ("src/app.py", call_start, call_start + len(b"callee"))
    ]
    assert resolved == definitions
    assert verified is True
    assert all(not Path(item.path).is_absolute() for item in (*definitions, *calls))
    assert all(item.provenance[0].source == "graph" for item in (*definitions, *calls))
    assert reads
    assert all(max_bytes == 16 * 1024 * 1024 for _path, max_bytes, _deadline in reads)
    assert all(read_deadline == deadline for _path, _max, read_deadline in reads)


def test_navigation_source_bytes_uses_retained_containment_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lsp_security
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    captured = {}
    deadline = time.monotonic() + 5

    def read_source(repository, relative_path, *, max_bytes, deadline):
        captured.update(
            repository=repository,
            relative_path=relative_path,
            max_bytes=max_bytes,
            deadline=deadline,
        )
        return b"value = 1\n"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("navigation source reopened a pathname")

    monkeypatch.setattr(lsp_security, "read_repository_source_bytes", read_source)
    monkeypatch.setattr(lsp_security, "resolve_repository_source", forbidden)
    monkeypatch.setattr(mcp_server, "read_stable_bytes", forbidden)

    assert mcp_server._navigation_source_bytes(
        scope,
        "source.py",
        deadline=deadline,
    ) == b"value = 1\n"
    assert captured == {
        "repository": scope,
        "relative_path": "source.py",
        "max_bytes": mcp_server.MAX_NAVIGATION_SOURCE_BYTES,
        "deadline": deadline,
    }


def test_navigation_location_requires_matching_source_and_evidence_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    content = b"def api():\n    return 1\n"
    scope = resolve_repository_scope(tmp_path)
    source_hash = hashlib.sha256(content).hexdigest()
    start = content.index(b"api")
    span = {
        "relative_path": "api.py",
        "role": "definition",
        "byte_start": start,
        "byte_end": start + len(b"api"),
        "source_sha256": source_hash,
        "span_sha256": hashlib.sha256(b"api").hexdigest(),
    }
    monkeypatch.setattr(
        mcp_server,
        "_navigation_source_bytes",
        lambda *_args, **_kwargs: content,
    )

    assert mcp_server._navigation_location_from_span(
        scope,
        span,
        source_kind="evidence",
        require_span_hash=True,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + 5,
    ) is not None
    assert mcp_server._navigation_location_from_span(
        scope,
        {**span, "source_sha256": "0" * 64},
        source_kind="evidence",
        require_span_hash=True,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + 5,
    ) is None
    assert mcp_server._navigation_location_from_span(
        scope,
        {**span, "span_sha256": "0" * 64},
        source_kind="evidence",
        require_span_hash=True,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + 5,
    ) is None
    assert mcp_server._navigation_location_from_span(
        scope,
        {**span, "span_sha256": "A" * 64},
        source_kind="evidence",
        require_span_hash=True,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + 5,
    ) is None
    assert mcp_server._navigation_location_from_span(
        scope,
        {key: value for key, value in span.items() if key != "span_sha256"},
        source_kind="evidence",
        require_span_hash=True,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + 5,
    ) is None
    assert mcp_server._navigation_location_from_span(
        scope,
        {key: value for key, value in span.items() if key != "span_sha256"},
        source_kind="occurrence",
        require_span_hash=False,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + 5,
    ) is not None


def test_navigation_graph_callback_reads_each_hash_bound_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import mcp_server
    from repository_scope import resolve_repository_scope

    content = b"def alpha(): pass\ndef beta(): pass\n"
    source_hash = hashlib.sha256(content).hexdigest()
    scope = resolve_repository_scope(tmp_path)
    reads = 0

    class Graph:
        repository_scope = scope
        generation_id = "generation-1"

        def find_nodes(self, **_kwargs):
            return [{"node_id": "node", "metadata": {"name": "alpha"}}]

        def occurrences(self, *_args, **_kwargs):
            return [
                {
                    "relative_path": "api.py",
                    "role": "definition",
                    "byte_start": content.index(name),
                    "byte_end": content.index(name) + len(name),
                    "source_sha256": source_hash,
                }
                for name in (b"alpha", b"beta")
            ]

        def close(self):
            return None

    def read_source(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return content

    monkeypatch.setattr(
        code_graph,
        "_active_evidence_graph",
        lambda _root, **_options: Graph(),
    )
    monkeypatch.setattr(mcp_server, "_navigation_source_bytes", read_source)

    locations = mcp_server._graph_declaration_locations(
        "alpha",
        scope,
        time.monotonic() + 5,
    )

    assert len(locations) == 2
    assert reads == 1
    assert mcp_server.MAX_NAVIGATION_GRAPH_FACTS == 10_000
    assert mcp_server.MAX_NAVIGATION_SOURCE_CACHE_BYTES == 64 * 1024 * 1024


def test_structural_callback_reuses_anchor_source_for_graph_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import mcp_server
    from code_intelligence import Capability
    from code_navigation import NavigationRequest
    from repository_scope import resolve_repository_scope

    content = b"def alpha(): pass\n"
    scope = resolve_repository_scope(tmp_path)
    source_hash = hashlib.sha256(content).hexdigest()
    reads = 0

    class Graph:
        repository_scope = scope
        generation_id = "generation-1"

        def find_nodes(self, **_kwargs):
            return [{"node_id": "alpha", "metadata": {"name": "alpha"}}]

        def occurrences(self, *_args, **_kwargs):
            return [
                {
                    "relative_path": "api.py",
                    "role": "definition",
                    "byte_start": 4,
                    "byte_end": 9,
                    "line_start": 1,
                    "source_sha256": source_hash,
                }
            ]

        def close(self):
            return None

    def read_source(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return content

    monkeypatch.setattr(
        code_graph,
        "_active_evidence_graph",
        lambda _root, **_options: Graph(),
    )
    monkeypatch.setattr(mcp_server, "_navigation_source_bytes", read_source)
    request = NavigationRequest(
        scope,
        Capability.DEFINITIONS,
        "api.py",
        1,
        4,
    )

    assert len(
        mcp_server._navigation_structural_candidates(
            request,
            time.monotonic() + 5,
        )
    ) == 1
    assert reads == 1


def test_navigation_source_cache_remembers_byte_cap_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    reads = 0

    def read_source(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return b"12345"

    monkeypatch.setattr(mcp_server, "MAX_NAVIGATION_SOURCE_CACHE_BYTES", 4)
    monkeypatch.setattr(mcp_server, "_navigation_source_bytes", read_source)
    cache = mcp_server._NavigationSourceCache()

    assert cache.read(scope, "large.py", deadline=time.monotonic() + 5) is None
    assert cache.read(scope, "large.py", deadline=time.monotonic() + 5) is None
    assert reads == 1
    assert cache._bytes == 0
    assert len(cache._values) == 1


def test_navigation_calls_use_lightweight_evidence_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import mcp_server
    from repository_scope import resolve_repository_scope

    content = b"callee()\n"
    scope = resolve_repository_scope(tmp_path)
    span_calls = 0
    include_span_hash = True

    class Graph:
        repository_scope = scope
        generation_id = "generation-1"

        def find_nodes(self, **_kwargs):
            return [{"node_id": "callee", "metadata": {"name": "callee"}}]

        def edges(self, **_kwargs):
            return [
                {
                    "assertion_id": "call",
                    "source_node_id": "caller",
                    "target_node_id": "callee",
                }
            ]

        def evidence_spans(self, **_kwargs):
            nonlocal span_calls, include_span_hash
            span_calls += 1
            span = {
                "relative_path": "api.py",
                "byte_start": 0,
                "byte_end": len(b"callee"),
                "source_sha256": hashlib.sha256(content).hexdigest(),
            }
            if include_span_hash:
                span["span_sha256"] = hashlib.sha256(b"callee").hexdigest()
            return [span]

        def evidence(self, **_kwargs):
            raise AssertionError("navigation loaded source blobs from the graph")

        def close(self):
            return None

    monkeypatch.setattr(
        code_graph,
        "_active_evidence_graph",
        lambda _root, **_options: Graph(),
    )
    monkeypatch.setattr(
        mcp_server,
        "_navigation_source_bytes",
        lambda *_args, **_kwargs: content,
    )

    locations = mcp_server._graph_call_locations(
        "callee",
        scope,
        direction="incoming",
        deadline=time.monotonic() + 5,
    )
    include_span_hash = False
    missing_hash_locations = mcp_server._graph_call_locations(
        "callee",
        scope,
        direction="incoming",
        deadline=time.monotonic() + 5,
    )

    assert len(locations) == 1
    assert missing_hash_locations == ()
    assert span_calls == 2


def test_navigation_graph_open_is_read_only_and_deadline_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    captured = {}

    class Graph:
        repository_scope = scope

    graph = Graph()

    def open_graph(root, **options):
        captured.update(root=root, options=options)
        return graph

    monkeypatch.setattr(code_graph, "_active_evidence_graph", open_graph)
    deadline = time.monotonic() + 5

    assert mcp_server._open_navigation_graph(scope, deadline) is graph
    assert captured["root"] == Path(scope.checkout_root)
    assert captured["options"]["read_only"] is True
    assert captured["options"]["deadline"] == deadline
    assert callable(captured["options"]["cancelled"])


def test_navigation_adapter_returns_empty_when_exact_graph_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import mcp_server
    from code_intelligence import Capability
    from code_navigation import NavigationRequest
    from repository_scope import resolve_repository_scope

    source = tmp_path / "api.py"
    source.write_text("def api():\n    pass\n", encoding="utf-8")
    scope = resolve_repository_scope(tmp_path)
    monkeypatch.setattr(
        code_graph, "_active_evidence_graph", lambda _root, **_options: None
    )
    request = NavigationRequest(
        scope,
        Capability.IMPLEMENTATIONS,
        "api.py",
        1,
        4,
    )

    assert mcp_server._navigation_structural_candidates(
        request,
        time.monotonic() + 5,
    ) == ()


def test_navigation_adapter_checks_deadline_after_graph_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_graph
    import mcp_server
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    now = [100.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    class Graph:
        repository_scope = scope
        generation_id = "generation-17"

        def find_nodes(self, **_kwargs):
            now[0] = 101.0
            return []

        def close(self):
            return None

    monkeypatch.setattr(mcp_server, "time", Clock)
    monkeypatch.setattr(
        code_graph, "_active_evidence_graph", lambda _root, **_options: Graph()
    )

    with pytest.raises(TimeoutError):
        mcp_server._navigation_symbol_resolver("api", scope, 100.5)


def test_navigation_graph_evidence_work_is_globally_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    from lsp_positions import SourceAnchor
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(tmp_path)
    span_attempts = 0

    class Graph:
        generation_id = "generation-17"

        def find_nodes(self, **_kwargs):
            return [
                {"node_id": f"source-{index}", "metadata": {"name": "source"}}
                for index in range(3)
            ]

        def occurrences(self, *_args, **_kwargs):
            return [
                {"role": "definition", "invalid": index}
                for index in range(3)
            ]

    def location(*_args, **_kwargs):
        nonlocal span_attempts
        span_attempts += 1
        return None

    monkeypatch.setattr(mcp_server, "MAX_NAVIGATION_GRAPH_FACTS", 3)
    monkeypatch.setattr(mcp_server, "_navigation_location_from_span", location)

    assert mcp_server._graph_node_ids_at_anchor(
        Graph(),
        "source",
        SourceAnchor("source.py", 1, 0, 0),
        scope,
        time.monotonic() + 5,
    ) == set()
    assert span_attempts <= 3


def test_precise_dispatch_exception_returns_normalized_redacted_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    sensitive = (
        r"api_key=sk-abcdefghijklmnopqrstuvwxyz "
        r"C:\Users\operator\outside\secret.py"
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(sensitive)

    monkeypatch.setattr(mcp_server, "_get_precise_architecture", fail)
    text = asyncio.run(
        mcp_server._handle_tool_call(
            "get_architecture",
            {
                "directory": str(tmp_path),
                "mode": "definition",
                "path": "api.py",
                "line": 1,
                "character": 0,
            },
        )
    )
    envelope = json.loads(text)

    assert envelope["data"]["status"] == "error"
    assert envelope["data"]["mode"] == "definition"
    assert "error" not in envelope["data"]
    assert set(envelope["data"]) >= {
        "freshness",
        "provider",
        "repository",
        "groups",
        "diagnostics",
        "warnings",
    }
    assert sensitive not in text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text


def test_renderer_value_error_maps_to_normalized_navigation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code_navigation
    import code_navigation_renderer
    import mcp_server

    source = tmp_path / "api.py"
    source.write_text("def api():\n    pass\n", encoding="utf-8")

    class Session:
        identity = object()

    class Manager:
        def get(self, *_args, **_kwargs):
            return Session()

    class Navigation:
        def __init__(self, *_args, **_kwargs):
            pass

        def query(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(code_navigation, "CodeNavigation", Navigation)
    monkeypatch.setattr(
        code_navigation_renderer,
        "render_navigation",
        lambda _result: (_ for _ in ()).throw(ValueError("sensitive renderer detail")),
    )
    monkeypatch.setattr(
        mcp_server,
        "_navigation_session_manager",
        lambda _deadline, _expected_epoch, _cancelled: Manager(),
    )

    data = mcp_server._get_precise_architecture(
        str(tmp_path),
        mode="definition",
        path="api.py",
        line=1,
        character=4,
        deadline=time.monotonic() + 5,
    )

    assert data["status"] == "error"
    assert data["warnings"] == ("navigation_render_failed",)
    assert "error" not in data


def test_hard_precise_timeout_returns_normalized_timeout_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import mcp_server

    release = threading.Event()

    def blocked(*_args, **_kwargs):
        release.wait(1)
        return {"status": "ok"}

    monkeypatch.setattr(mcp_server, "MCP_LSP_STARTUP_SECONDS", 0.05)
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS", set())
    monkeypatch.setattr(mcp_server, "_MCP_WORKERS_LOCK", threading.Lock())
    monkeypatch.setattr(mcp_server, "_get_precise_architecture", blocked)
    try:
        text = asyncio.run(
            mcp_server._handle_tool_call(
                "get_architecture",
                {
                    "directory": str(tmp_path),
                    "mode": "references",
                    "path": "api.py",
                    "line": 1,
                    "character": 0,
                },
            )
        )
    finally:
        release.set()
    data = json.loads(text)["data"]

    assert data["status"] == "timeout"
    assert data["mode"] == "references"
    assert data["requested_capability"] == "references"
    assert data["warnings"] == ["navigation_timeout"]
    assert "error" not in data


def test_precise_timeout_envelope_is_static_and_probe_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    def forbidden(*_args, **_kwargs):
        raise AssertionError("timeout formatting invoked a normal response probe")

    monkeypatch.setattr(mcp_server, "_build_operation_envelope", forbidden)
    monkeypatch.setattr(mcp_server, "_quality_for", forbidden)
    monkeypatch.setattr(mcp_server, "_components_for", forbidden)
    monkeypatch.setattr(mcp_server, "_sanitize_navigation_data", forbidden)

    envelope = json.loads(
        mcp_server._tool_timeout_envelope_text(
            "get_architecture",
            {
                "directory": r"C:\private\repository",
                "mode": "definition",
                "path": "api.py",
                "line": 1,
                "character": 0,
            },
        )
    )

    assert set(envelope) == ENVELOPE_FIELDS
    assert envelope["source_commit"] is None
    assert envelope["index_timestamp"] is None
    assert envelope["components"] == {}
    assert envelope["partial"] is True
    assert envelope["warnings"] == ["navigation_timeout"]
    assert envelope["data"]["status"] == "timeout"
    assert envelope["data"]["directory"] is None


@pytest.mark.parametrize(
    ("status", "expected_partial"),
    [
        ("ok", False),
        ("partial", True),
        ("unsupported", True),
        ("not_ready", True),
        ("stale", True),
        ("timeout", True),
        ("error", True),
    ],
)
def test_precise_quality_uses_rendered_status_without_mutating_data(
    status: str,
    expected_partial: bool,
) -> None:
    import mcp_server

    data = mcp_server._normalized_navigation_failure(
        directory="C:/repo",
        mode="definition",
        status=status,
        warning="navigation_warning" if expected_partial else "",
        offset=0,
        limit=10,
    )
    if not expected_partial:
        data["warnings"] = ()
    before = dict(data)

    quality = mcp_server._quality_for(
        "get_architecture",
        data,
        {"mode": "definition"},
    )

    assert quality["partial"] is expected_partial
    assert quality["warnings"] == list(data["warnings"])
    assert data == before
    assert "_navigation_partial" not in data


def test_rendered_partial_status_drives_outer_envelope_without_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    data = mcp_server._normalized_navigation_failure(
        directory=str(tmp_path),
        mode="definition",
        status="partial",
        warning="output_token_bound",
        offset=0,
        limit=10,
    )
    monkeypatch.setattr(mcp_server, "_get_precise_architecture", lambda *_a, **_k: data)

    envelope = json.loads(
        asyncio.run(
            mcp_server._handle_tool_call(
                "get_architecture",
                {
                    "directory": str(tmp_path),
                    "mode": "definition",
                    "path": "api.py",
                    "line": 1,
                    "character": 0,
                },
            )
        )
    )

    assert envelope["partial"] is True
    assert envelope["data"]["status"] == "partial"
    assert "_navigation_partial" not in envelope["data"]
    assert "output_token_bound" in envelope["warnings"]


def test_navigation_text_fields_are_secret_and_external_path_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server

    sensitive = (
        r"api_key=sk-abcdefghijklmnopqrstuvwxyz "
        r"C:\Users\operator\outside\secret.py"
    )
    data = mcp_server._normalized_navigation_failure(
        directory=str(tmp_path),
        mode="diagnostics",
        status="partial",
        warning=sensitive,
        offset=0,
        limit=10,
    )
    data["hover"] = sensitive
    data["groups"] = [
        {
            "path": "api.py",
            "containing_symbol": None,
            "locations": [
                {
                    "path": "api.py",
                    "line": 1,
                    "character": 0,
                    "range": {"byte_start": 0, "byte_end": 3},
                    "containing_symbol": None,
                    "signature": sensitive,
                    "resolution": "lsp_confirmed",
                }
            ],
        }
    ]
    data["diagnostics"] = [
        {
            "path": "api.py",
            "range": {"byte_start": 0, "byte_end": 3},
            "severity": "error",
            "code": sensitive,
            "message": sensitive,
            "related": [data["groups"][0]["locations"][0]],
        }
    ]
    monkeypatch.setattr(mcp_server, "_get_precise_architecture", lambda *_a, **_k: data)

    text = asyncio.run(
        mcp_server._handle_tool_call(
            "get_architecture",
            {
                "directory": str(tmp_path),
                "mode": "diagnostics",
                "path": "api.py",
                "line": 1,
                "character": 0,
            },
        )
    )
    rendered = json.loads(text)["data"]

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
    assert r"C:\Users\operator\outside\secret.py" not in text
    assert rendered["groups"][0]["locations"][0]["signature"]
    assert rendered["diagnostics"][0]["message"]
    assert rendered["diagnostics"][0]["code"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered["diagnostics"][0]["code"]
    assert r"C:\Users\operator\outside\secret.py" not in rendered["diagnostics"][0]["code"]
    assert rendered["diagnostics"][0]["related"][0]["signature"]
    assert rendered["hover"]
    assert rendered["groups"][0]["path"] == "api.py"


def test_navigation_text_sanitizer_never_slices_a_redaction_marker() -> None:
    import mcp_server

    value = "x /a/b"
    sanitized = mcp_server._sanitize_navigation_text(value)

    assert sanitized == "x "
    assert len(sanitized.encode("utf-8")) <= len(value.encode("utf-8"))
    assert "[PAT" not in sanitized


def test_precise_components_report_provider_and_graph_without_private_state() -> None:
    import mcp_server

    data = mcp_server._normalized_navigation_failure(
        directory="C:/repo",
        mode="definition",
        status="ok",
        warning="",
        offset=0,
        limit=10,
        provider="pyright",
        provider_version="1.1.411",
        readiness="query_ready",
    )
    data["warnings"] = ()
    data["provenance"] = [
        {
            "source": "graph",
            "provider": "evidence-graph",
            "version": "generation-17",
            "observation": "graph_candidate",
        }
    ]

    assert mcp_server._components_for("get_architecture", data) == {
        "provider": {"generation": "1.1.411", "freshness": "fresh"},
        "graph": {"generation": "generation-17", "freshness": "fresh"},
    }
