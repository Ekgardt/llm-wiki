# Tests of `scripts/code_graph.py`

- `tests/test_a_code_root_named_knowledge_is_code.py`: `knowledge/` is this vault's noun, not a fact about every repository (G-M5).

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_code_root_named_knowledge_is_code.py -q`.
