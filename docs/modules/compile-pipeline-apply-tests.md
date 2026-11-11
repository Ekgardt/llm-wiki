# Tests of `scripts/llm_wiki/compile_pipeline_apply.py`

- `tests/native_compile_uses_whole_container_helpers.py`: Helpers shared by test_native_compile_uses_whole_container.py and the test files importing them.
- `tests/test_the_vault_log_rotates_before_its_cap.py`: A compile archives and restarts the vault log instead of failing at its cap.

`scripts/llm_wiki/compile_pipeline_apply.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/native_compile_uses_whole_container_helpers.py tests/test_the_vault_log_rotates_before_its_cap.py -q`.
