# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_context_reuses_exact_source_choices.py`: Optional context must not repeatedly derive the same complete source choices.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_context_reuses_exact_source_choices.py -q`.
