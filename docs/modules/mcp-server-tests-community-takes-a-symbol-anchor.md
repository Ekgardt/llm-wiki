# Tests of `scripts/mcp_server.py`

- `tests/test_community_takes_a_symbol_anchor.py`: `mode=community` accepts a symbol anchor at the MCP boundary.

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_community_takes_a_symbol_anchor.py -q`.
