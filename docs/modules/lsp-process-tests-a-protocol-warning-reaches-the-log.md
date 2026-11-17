# Tests filed with `scripts/lsp_process.py`

- `tests/test_a_protocol_warning_reaches_the_log.py`: A language-server protocol warning reaches stderr, not a callback nobody set.

These files do not import `scripts/lsp_process.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/lsp_process_generation.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_protocol_warning_reaches_the_log.py -q`.
