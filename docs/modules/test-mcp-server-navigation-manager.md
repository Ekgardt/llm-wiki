# `tests/test_mcp_server_navigation_manager.py`

The MCP server owns one navigation manager: its lock honours deadlines, it refuses cancelled and stale requests, and close resets it only after success.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_mcp_server_navigation_manager.py -q`.
