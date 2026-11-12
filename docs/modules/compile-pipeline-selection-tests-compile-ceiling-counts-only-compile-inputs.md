# Tests of `scripts/llm_wiki/compile_pipeline_selection.py`

- `tests/test_compile_ceiling_counts_only_compile_inputs.py`: Health forecasts the compiler's inputs, rather than the receipt directory.

`scripts/llm_wiki/compile_pipeline_selection.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_ceiling_counts_only_compile_inputs.py -q`.
