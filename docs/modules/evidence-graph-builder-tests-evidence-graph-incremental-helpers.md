# Tests of `scripts/evidence_graph_builder.py`

- `tests/evidence_graph_incremental_helpers.py`: Helpers shared by test_evidence_graph_incremental.py and the test files importing them.

`scripts/evidence_graph_builder.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/evidence_graph_incremental_helpers.py -q`.
