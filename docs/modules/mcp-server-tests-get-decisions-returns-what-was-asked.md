# Tests of `scripts/mcp_server.py`

- `tests/test_get_decisions_returns_what_was_asked.py`: `get_decisions` returns up to `limit` decision pages, one agent-shaped row each.

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_get_decisions_returns_what_was_asked.py -q`.
