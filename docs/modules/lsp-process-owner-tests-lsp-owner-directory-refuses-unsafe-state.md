# Tests of `scripts/llm_wiki/lsp_process_owner.py`

- `tests/test_lsp_owner_directory_refuses_unsafe_state.py`

`scripts/llm_wiki/lsp_process_owner.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_owner_directory_refuses_unsafe_state.py -q`.
