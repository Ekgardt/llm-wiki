"""What an agent's model reads of the tool list has a budget (audit 2026-09-26 C-10).

docs/research/2026-09-26-the-tool-list-has-a-token-budget.md
"""
from __future__ import annotations

import json

import mcp_server

# Measured 2026-09-26: 9 486 bytes of names, descriptions and input schemas for
# twelve tools. Growing past this is a decision, made by changing the number.
MODEL_FACING_BUDGET_BYTES = 10 * 1024


def _model_facing_bytes(tool) -> int:
    """The parts a Claude tool definition carries: name, description, input schema."""
    return len(tool.name) + len(tool.description or "") + len(json.dumps(tool.inputSchema))


def test_the_model_facing_tool_list_stays_within_its_budget() -> None:
    tools = mcp_server._build_tool_definitions()

    assert len(tools) == len(mcp_server._TOOL_HANDLERS) + 1  # doctor has its own handler
    assert sum(map(_model_facing_bytes, tools)) <= MODEL_FACING_BUDGET_BYTES
