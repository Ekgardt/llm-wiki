# Tests of `scripts/lsp_process.py`

- `tests/test_a_native_server_copy_leaves_with_its_owner.py`: A native server's verified copy leaves with its owner root (audit 2026-09-26 A-6, C-7).

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_native_server_copy_leaves_with_its_owner.py -q`.
