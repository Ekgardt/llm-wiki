# Tests of `scripts/mcp_server.py`

- `tests/test_the_tool_list_has_a_token_budget.py`: What an agent's model reads of the tool list has a budget (audit 2026-09-26 C-10).

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_tool_list_has_a_token_budget.py -q`.
