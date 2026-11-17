# Tests filed with `scripts/mcp_server.py`

- `tests/test_shared_server_warms_the_same_way.py`: The shared server warmed once, and one pass records the wrong cost.

These files do not import `scripts/mcp_server.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/mcp_http.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_shared_server_warms_the_same_way.py -q`.
