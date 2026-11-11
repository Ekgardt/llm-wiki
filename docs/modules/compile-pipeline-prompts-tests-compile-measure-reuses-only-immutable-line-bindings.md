# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_measure_reuses_only_immutable_line_bindings.py`: Repeated sizing reuses pure bindings; protection and publication stay fresh.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_measure_reuses_only_immutable_line_bindings.py -q`.
