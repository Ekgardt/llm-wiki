# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_related_links_share_the_response_budget.py`: Correct links fit the bounded plan regardless of their separate count.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_related_links_share_the_response_budget.py -q`.
