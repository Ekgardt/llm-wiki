# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_repair_feedback_cannot_use_an_old_cache.py`: A cached plan cannot complete newly reviewed correction work without its feedback.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_repair_feedback_cannot_use_an_old_cache.py -q`.
