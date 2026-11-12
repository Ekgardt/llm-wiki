# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

`tests/test_emitted_compile_layout_owns_immutable_parsing.py` exercise `scripts/llm_wiki/compile_pipeline_prompts.py` through its public names.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_emitted_compile_layout_owns_immutable_parsing.py -q`.
