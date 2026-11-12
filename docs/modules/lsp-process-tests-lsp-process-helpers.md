# Tests of `scripts/lsp_process.py`

- `tests/lsp_process_helpers.py`: Helpers shared by test_lsp_process.py and the test files importing them.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/lsp_process_helpers.py -q`.
