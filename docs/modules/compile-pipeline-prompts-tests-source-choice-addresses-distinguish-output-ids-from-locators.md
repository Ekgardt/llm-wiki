# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_source_choice_addresses_distinguish_output_ids_from_locators.py`: Source IDs and visible FILE row locators must have explicit different roles.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_source_choice_addresses_distinguish_output_ids_from_locators.py -q`.
