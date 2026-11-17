# Tests filed with `scripts/memory_state.py`

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/evidence_reader_cache.py`: its exact derived description.
- `scripts/mcp_server.py`: its exact derived description.
- `scripts/repository_indexing.py`: its exact derived description.
- `scripts/symbol_snippet.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_definition_and_a_service_walk_say_what_they_left_out.py -q`.
