# Tests of `scripts/llm_wiki/compile_pipeline_selection.py`

`tests/test_compile_request_selection.py` exercise `scripts/llm_wiki/compile_pipeline_selection.py` through its public names.

`scripts/llm_wiki/compile_pipeline_selection.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_request_selection.py -q`.
