# Tests of `scripts/memory_state.py`

- `tests/a_name_is_resolved_before_the_graph_is_asked_helpers.py`: Helpers shared by test_a_name_is_resolved_before_the_graph_is_asked.py and the test files importing them.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_name_is_resolved_before_the_graph_is_asked_helpers.py -q`.
