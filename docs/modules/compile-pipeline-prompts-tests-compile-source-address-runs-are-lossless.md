# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_source_address_runs_are_lossless.py`: Range display preserves every authenticated address, including sparse gaps.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_source_address_runs_are_lossless.py -q`.
