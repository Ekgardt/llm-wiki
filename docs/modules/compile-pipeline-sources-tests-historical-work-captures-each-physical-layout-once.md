# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

`tests/test_historical_work_captures_each_physical_layout_once.py` exercise `scripts/llm_wiki/compile_pipeline_sources.py` through its public names.

`scripts/llm_wiki/compile_pipeline_sources.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_historical_work_captures_each_physical_layout_once.py -q`.
