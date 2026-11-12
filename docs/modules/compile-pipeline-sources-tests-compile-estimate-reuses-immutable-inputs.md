# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/test_compile_estimate_reuses_immutable_inputs.py`: Packing must not repeatedly render the immutable schema or scan unrelated days.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_estimate_reuses_immutable_inputs.py -q`.
