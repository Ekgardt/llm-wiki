# Tests filed with `scripts/code_graph.py`

- `tests/test_every_architecture_mode_is_bounded.py`: Every get_architecture mode runs on the bounded code-graph workers under the deadline.

These files do not import `scripts/code_graph.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/mcp_server.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_architecture_mode_is_bounded.py -q`.
