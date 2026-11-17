# Tests of `scripts/llm_wiki/compile_pipeline_batches.py`

- `tests/test_reviewed_context_cannot_be_reselected_during_repair.py`: A higher-ranked new page cannot replace context already used by the critic.

`scripts/llm_wiki/compile_pipeline_batches.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_reviewed_context_cannot_be_reselected_during_repair.py -q`.
