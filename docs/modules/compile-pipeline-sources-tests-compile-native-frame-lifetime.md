# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/test_compile_native_frame_lifetime.py`: Verified native capture retains coordinates, not every decoded payload.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_native_frame_lifetime.py -q`.
