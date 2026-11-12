# Tests of `scripts/llm_wiki/compile_pipeline_batches.py`

- `tests/test_compile_uses_the_attempt_model_budget.py`: A batch budget cannot outrun the provider actually asked to process it.

`scripts/llm_wiki/compile_pipeline_batches.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_uses_the_attempt_model_budget.py -q`.
