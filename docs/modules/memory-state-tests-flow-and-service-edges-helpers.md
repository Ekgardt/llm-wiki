# Tests filed with `scripts/memory_state.py`

- `tests/flow_and_service_edges_helpers.py`: Helpers shared by test_flow_and_service_edges.py and the test files importing them.

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/code_graph.py`: its exact derived description.
- `scripts/evidence_reader_cache.py`: its exact derived description.
- `scripts/repository_indexing.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/flow_and_service_edges_helpers.py -q`.
