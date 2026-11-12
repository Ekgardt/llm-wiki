# Tests of `scripts/code_graph.py`

- `tests/test_every_architecture_mode_is_bounded.py`: Every get_architecture mode runs on the bounded code-graph workers under the deadline.

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_architecture_mode_is_bounded.py -q`.
