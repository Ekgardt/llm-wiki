# Tests of `scripts/llm_client.py`

- `tests/test_internal_codex_does_not_start_operator_mcp.py`: Closed memory packets must not start the operator's configured MCP servers.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_internal_codex_does_not_start_operator_mcp.py -q`.
