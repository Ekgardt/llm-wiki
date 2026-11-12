# Tests of `scripts/llm_wiki/compile_pipeline_batches.py`

`tests/test_compile_resolution_failure_progress.py` exercise `scripts/llm_wiki/compile_pipeline_batches.py` through its public names.

`scripts/llm_wiki/compile_pipeline_batches.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_resolution_failure_progress.py -q`.
