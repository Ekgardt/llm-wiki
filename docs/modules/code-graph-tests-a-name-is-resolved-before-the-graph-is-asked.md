# Tests of `scripts/code_graph.py`

- `tests/test_a_name_is_resolved_before_the_graph_is_asked.py`: Audit 3, A5-A7: names are resolved, cut and sliced before the reader is asked.

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_name_is_resolved_before_the_graph_is_asked.py -q`.
