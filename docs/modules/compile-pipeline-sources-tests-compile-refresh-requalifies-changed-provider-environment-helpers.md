# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/compile_refresh_requalifies_changed_provider_environment_helpers.py`: Helpers shared by test_compile_refresh_requalifies_changed_provider_environment.py and the test files importing them.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_refresh_requalifies_changed_provider_environment_helpers.py -q`.
