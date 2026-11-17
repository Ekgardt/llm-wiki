# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_critic_feedback_uses_verified_attempt_capacity.py`: A source packing target must not prevent a repair the selected model can fit.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_critic_feedback_uses_verified_attempt_capacity.py -q`.
