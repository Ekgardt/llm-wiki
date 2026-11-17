# Tests of `scripts/evidence_graph.py`

- `tests/whole_graph_aggregates_helpers.py`: Helpers shared by test_whole_graph_aggregates.py and the test files importing them.

`scripts/evidence_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/whole_graph_aggregates_helpers.py -q`.
