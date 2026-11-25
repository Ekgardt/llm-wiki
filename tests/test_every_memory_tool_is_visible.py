"""Every memory tool is in the agent's tool list from the first turn.

Claude Code and Codex both defer MCP tools behind a tool search by default, so
an agent had to search before it could see `recall` at all. Claude Code loads a
tool whose `_meta` carries `anthropic/alwaysLoad`; Codex shows a server's tools
directly when its entry says `omit_tools_from = ["deferred"]`
(docs/research/2026-11-17-every-memory-tool-is-visible.md).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

# What the always-loaded list costs every turn: name, description and input
# schema of the 12 tools measured 10 755 bytes on 2026-11-17. The budget leaves
# room for one small tool; growing past it is a decision, not a side effect.
MODEL_FACING_BUDGET_BYTES = 12 * 1024


def _listed_tools() -> list[dict[str, object]]:
    import mcp_server

    return [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in mcp_server._build_tool_definitions()
    ]


def test_every_tool_asks_claude_code_to_load_it_with_the_session() -> None:
    tools = _listed_tools()

    assert len(tools) == 12
    assert all(tool.get("_meta") == {"anthropic/alwaysLoad": True} for tool in tools), [
        tool["name"] for tool in tools if tool.get("_meta") != {"anthropic/alwaysLoad": True}
    ]


def test_the_always_loaded_list_stays_within_its_budget() -> None:
    model_facing = [
        {key: tool[key] for key in ("name", "description", "inputSchema") if key in tool}
        for tool in _listed_tools()
    ]

    assert len(json.dumps(model_facing)) <= MODEL_FACING_BUDGET_BYTES


# Where each installer's written Codex block opens and closes: the printf that
# builds it in install.sh, the here-string in install.ps1. Anchoring on the block
# itself keeps a warning that quotes the line from standing in for it.
_WRITTEN_BLOCK = {
    "install.sh": ('block="$(printf', 'write_codex_mcp_block "$block"'),
    "install.ps1": ('$block = @"', '"@'),
}


def _written_codex_block(installer: str) -> str:
    text = (ROOT / installer).read_text(encoding="utf-8")
    opening, closing = _WRITTEN_BLOCK[installer]
    assert text.count(opening) == 1, installer
    start = text.index(opening)
    return text[start : text.index(closing, start + len(opening))]


def test_both_installers_keep_codex_from_deferring_the_tools() -> None:
    for installer in _WRITTEN_BLOCK:
        block = _written_codex_block(installer)
        assert "[mcp_servers.llm-wiki]" in block, installer
        line = r"^\s*'?omit_tools_from = \[\"deferred\"\]('\)\")?$"
        assert re.search(line, block, re.M), installer


def test_an_installed_codex_entry_with_the_line_is_equivalent(tmp_path: Path) -> None:
    import codex_memory

    config = tmp_path / "config.toml"
    args = json.dumps(codex_memory._mcp_expected_args(tmp_path))
    config.write_text(
        f'[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = {args}\n'
        'omit_tools_from = ["deferred"]\n',
        encoding="utf-8",
    )

    assert codex_memory.codex_mcp_config_state(config, tmp_path) == "equivalent"
