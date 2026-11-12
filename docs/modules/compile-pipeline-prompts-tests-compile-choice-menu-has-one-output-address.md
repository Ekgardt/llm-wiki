# Tests of `scripts/llm_wiki/compile_pipeline_prompts.py`

- `tests/test_compile_choice_menu_has_one_output_address.py`: Delivered source addresses distinguish output IDs from visible LF locators.

`scripts/llm_wiki/compile_pipeline_prompts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_choice_menu_has_one_output_address.py -q`.
