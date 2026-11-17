# Tests of `scripts/llm_wiki/compile_pipeline_sources.py`

- `tests/test_compile_binds_the_protected_source_view.py`: A protected source view never substitutes for original physical authority.

`scripts/llm_wiki/compile_pipeline_sources.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_binds_the_protected_source_view.py -q`.
