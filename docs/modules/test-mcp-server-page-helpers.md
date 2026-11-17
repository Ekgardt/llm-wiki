# `tests/test_mcp_server_page_helpers.py`

The MCP page helpers read flat pages, resolve and cap content-addressed evidence, fail closed and redacted, and record reads without content.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_mcp_server_page_helpers.py -q`.
