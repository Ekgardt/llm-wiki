# Tests of `scripts/evidence_graph_builder.py`

- `tests/test_generation_rebuild_reuse.py`: A rebuild must reuse what did not change, and prove it changed nothing.

`scripts/evidence_graph_builder.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_generation_rebuild_reuse.py -q`.
