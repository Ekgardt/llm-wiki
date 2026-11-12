"""Helpers shared by test_an_earlier_codex_entry_is_rewritten.py and the test files importing them."""
from __future__ import annotations

EARLIER = (
    '# my settings\nmodel = "gpt-5.6"\n\n'
    "[mcp_servers.llm-wiki]\n"
    'command = "uv"\n'
    'args = ["run", "--directory", "/srv/old-vault", "python", "scripts/mcp_server.py"]\n'
    "\n# the other server\n"
    "[mcp_servers.other]\n"
    'command = "other"\n'
)
