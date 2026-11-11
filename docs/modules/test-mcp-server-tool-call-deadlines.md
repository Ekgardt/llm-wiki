# `tests/test_mcp_server_tool_call_deadlines.py`

A tool call runs under one absolute handler deadline: workers stay bounded, timed-out work never lands late, and late failures stay observable without secrets.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_mcp_server_tool_call_deadlines.py -q`.
