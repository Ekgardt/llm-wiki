# Tests of `scripts/evidence_graph_builder.py`

- `tests/test_a_code_generation_needs_no_vectors.py`: A code generation is built without vectors; a memory generation still carries them.

`scripts/evidence_graph_builder.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_code_generation_needs_no_vectors.py -q`.
