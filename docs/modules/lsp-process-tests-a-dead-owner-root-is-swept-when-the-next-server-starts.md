# Tests of `scripts/lsp_process.py`

- `tests/test_a_dead_owner_root_is_swept_when_the_next_server_starts.py`: Starting a server takes down the owner roots a dead one left behind.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dead_owner_root_is_swept_when_the_next_server_starts.py -q`.
