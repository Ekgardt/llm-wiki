# Tests filed with `scripts/lsp_process.py`

These files do not import `scripts/lsp_process.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/lsp_process_cleanup.py`: its exact derived description.
- `scripts/llm_wiki/lsp_process_state.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_stderr_wake_does_not_close_reused_descriptors.py -q`.
