# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/compile_critic_drop_cannot_complete_source_work_helpers.py`: Helpers shared by test_compile_critic_drop_cannot_complete_source_work.py and the test files importing them.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_critic_drop_cannot_complete_source_work_helpers.py -q`.
