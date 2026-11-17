# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/test_native_compile_uses_whole_container.py`: Native semantic compilation preserves physical proof across source parts.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_native_compile_uses_whole_container.py -q`.
