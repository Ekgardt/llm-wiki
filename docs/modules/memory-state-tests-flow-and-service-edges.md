# Tests of `scripts/memory_state.py`

- `tests/test_flow_and_service_edges.py`: Issue #24, B3: argument bindings hop by hop over a real generation.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_flow_and_service_edges.py -q`.
