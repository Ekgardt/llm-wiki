# Tests of `scripts/lsp_process.py`

- `tests/test_the_lsp_sweep_reaches_every_dead_root.py`: The sweep of `run/lsp` reaches every dead root and proves death by start identity.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_lsp_sweep_reaches_every_dead_root.py -q`.
