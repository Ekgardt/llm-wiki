# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/a_continuation_keeps_its_original_entry_helpers.py`: Helpers shared by test_a_continuation_keeps_its_original_entry.py and the test files importing them.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_continuation_keeps_its_original_entry_helpers.py -q`.
