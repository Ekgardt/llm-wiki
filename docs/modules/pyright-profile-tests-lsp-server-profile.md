# Tests of `scripts/pyright_profile.py`

- `tests/test_lsp_server_profile.py`: The language-neutral seam, and that Pyright through it is the same Pyright.

`scripts/pyright_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_server_profile.py -q`.
