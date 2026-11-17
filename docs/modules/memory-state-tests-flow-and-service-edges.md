# Tests filed with `scripts/memory_state.py`

- `tests/test_flow_and_service_edges.py`: Issue #24, B3: argument bindings hop by hop over a real generation.

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/code_graph.py`: its exact derived description.
- `scripts/evidence_reader_cache.py`: its exact derived description.
- `scripts/repository_indexing.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_flow_and_service_edges.py -q`.
