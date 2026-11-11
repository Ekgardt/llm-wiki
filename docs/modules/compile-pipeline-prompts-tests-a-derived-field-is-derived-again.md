# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_a_derived_field_is_derived_again.py`: Validating a validated compile operation gives the same operation (audit 2026-09-27 A-2).

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_derived_field_is_derived_again.py -q`.
