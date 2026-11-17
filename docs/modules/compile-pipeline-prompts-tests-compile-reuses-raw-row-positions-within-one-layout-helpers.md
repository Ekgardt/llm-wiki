# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/compile_reuses_raw_row_positions_within_one_layout_helpers.py`: Helpers shared by test_compile_reuses_raw_row_positions_within_one_layout.py and the test files importing them.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_reuses_raw_row_positions_within_one_layout_helpers.py -q`.
