"""The twelve MCP tools are described by names, schemas and bounds an agent can rely on."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.mcp_server_support import (
    ENVELOPE_FIELDS,
    _assert_context_array_bounds,
    _assert_directory_is_the_only_requirement,
    _assert_doctor_branch_actions,
    _assert_doctor_branch_shape,
    _assert_query_and_slug_bounds,
    _assert_task_shaped_names,
    _canonical_architecture_modes,
)


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
        from mcp_server import build_tool_definitions
        tools = build_tool_definitions()
        # Returns empty list if mcp not installed, or list of Tool objects
        assert isinstance(tools, list)

    def test_twelve_tools_defined(self):
        """Should define exactly 12 task-shaped tools."""
        from mcp_server import build_tool_definitions
        tools = build_tool_definitions()
        if tools:  # Only check if mcp package is installed
            assert len(tools) == 12

    def test_tool_names_are_task_shaped(self):
        """Tools should be named after tasks, not entities."""
        from mcp_server import build_tool_definitions
        tools = build_tool_definitions()
        if tools:
            _assert_task_shaped_names([t.name for t in tools])

    def test_doctor_has_closed_exact_action_branches(self):
        import mcp_server

        schema = mcp_server.TOOL_INPUT_SCHEMAS["doctor"]
        _assert_doctor_branch_shape(schema)
        _assert_doctor_branch_actions(schema["oneOf"])

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

        assert mcp_server.validate_tool_arguments("doctor", arguments)

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

        assert mcp_server.validate_tool_arguments("doctor", arguments) is None

    def test_find_dead_code_requires_a_directory(self):
        from mcp_server import build_tool_definitions

        tools = build_tool_definitions()
        if tools:
            _assert_directory_is_the_only_requirement(tools, "find_dead_code")

    def test_get_architecture_requires_a_directory(self):
        from mcp_server import build_tool_definitions

        tools = build_tool_definitions()
        if tools:
            _assert_directory_is_the_only_requirement(tools, "get_architecture")

    def test_get_architecture_declares_all_canonical_modes_without_new_tool(self):
        import mcp_server

        # Compared against the structures that actually serve a mode instead of
        # against a copied list, because a copied list goes stale silently and
        # did: provenance, snippet and coverage shipped 2026-08-28 with working
        # handlers and no enum entry. That is not merely undocumented —
        # validate_object_schema enforces this enum, so an unlisted mode is
        # refused at the door. Deriving the expectation makes the next mode
        # that forgets the enum fail here instead of shipping unreachable.
        modes = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]["properties"]["mode"]
        canonical = _canonical_architecture_modes(mcp_server)
        assert set(modes["enum"]) == canonical
        assert len(modes["enum"]) == len(canonical)
        tools = mcp_server.build_tool_definitions()
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

        tools = mcp_server.build_tool_definitions()
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

        assert mcp_server.validate_tool_arguments("recall", arguments)

    @pytest.mark.parametrize(
        "arguments",
        [
            {"query": "test", "profile": "BASE"},
            {"query": "test", "grounded": False, "profile": "BASE"},
        ],
    )
    def test_recall_rejects_profile_without_grounded_true(self, arguments):
        import mcp_server

        error = mcp_server.validate_tool_arguments("recall", arguments)

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
        # `content_preview` went with the context compiler on 2026-07-18 (audit C-20).
        assert "ignored" in include["description"]

    def test_retrieval_schemas_declare_hard_string_and_array_bounds(self):
        import mcp_server

        schemas = mcp_server.TOOL_INPUT_SCHEMAS
        _assert_query_and_slug_bounds(schemas)
        _assert_context_array_bounds(
            schemas["get_context"]["properties"]["slugs"],
            schemas["get_context"]["properties"]["include"],
        )
