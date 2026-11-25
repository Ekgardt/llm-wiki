"""An agent learns when to reach for the memory before it learns what a tool returns.

An agent chooses a tool by its description and meets a server through its
initialize instructions; Claude Code loads both with the session and cuts each
at 4,096 characters.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import mcp_server  # noqa: E402

CLAUDE_CODE_CUT_CHARACTERS = 4096


def test_every_tool_description_opens_with_when_to_call_it() -> None:
    descriptions = {tool.name: tool.description for tool in mcp_server._build_tool_definitions()}

    assert len(descriptions) == 12
    assert [name for name, text in descriptions.items() if not text.startswith("Call ")] == []


def test_the_server_tells_an_agent_when_to_use_the_memory() -> None:
    options = mcp_server.build_server().create_initialization_options()

    assert options.instructions == mcp_server.SERVER_INSTRUCTIONS
    assert len(options.instructions) <= CLAUDE_CODE_CUT_CHARACTERS


def test_the_instructions_name_only_tools_the_server_lists() -> None:
    named = set(re.findall(r"\b[a-z]+_[a-z_]+\b", mcp_server.SERVER_INSTRUCTIONS))

    assert named <= set(mcp_server.TOOL_DESCRIPTIONS)
    assert "call recall first" in mcp_server.SERVER_INSTRUCTIONS
