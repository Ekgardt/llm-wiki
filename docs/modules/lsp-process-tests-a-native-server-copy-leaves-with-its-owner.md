# Tests filed with `scripts/lsp_process.py`

- `tests/test_a_native_server_copy_leaves_with_its_owner.py`: A native server's verified copy leaves with its owner root (audit 2026-09-26 A-6, C-7).

These files do not import `scripts/lsp_process.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/lsp_process_owner.py`: its exact derived description.
- `scripts/llm_wiki/lsp_process_sweep.py`: its exact derived description.
- `scripts/process_liveness.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_native_server_copy_leaves_with_its_owner.py -q`.
