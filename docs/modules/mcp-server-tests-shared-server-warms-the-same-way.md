# Tests of `scripts/mcp_server.py`

- `tests/test_shared_server_warms_the_same_way.py`: The shared server warmed once, and one pass records the wrong cost.

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_shared_server_warms_the_same_way.py -q`.
