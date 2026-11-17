# Tests of `scripts/lsp_protocol.py`

- `tests/fake_lsp_server.py`: In-process hostile LSP peer used only by protocol tests.

`scripts/lsp_protocol.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/fake_lsp_server.py -q`.
