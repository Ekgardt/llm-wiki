# Tests filed with `scripts/memory_state.py`

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/mcp_server.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_answer_is_stale_only_when_a_source_changed.py -q`.
