# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_dense_choice_membership_preserves_budget.py`: Dense valid source lines must not spend the input budget on repeated IDs.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_dense_choice_membership_preserves_budget.py -q`.
