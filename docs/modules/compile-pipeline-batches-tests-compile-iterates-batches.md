# Tests of `scripts/llm_wiki/compile_pipeline_batches.py`

- `tests/test_compile_iterates_batches.py`: Incremental planning must preserve atomic sources and exact eager batches.

`scripts/llm_wiki/compile_pipeline_batches.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_iterates_batches.py -q`.
