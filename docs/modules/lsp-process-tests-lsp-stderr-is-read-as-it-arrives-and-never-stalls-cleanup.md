# Tests of `scripts/lsp_process.py`

- `tests/test_lsp_stderr_is_read_as_it_arrives_and_never_stalls_cleanup.py`: The server's stderr is read as it arrives, and reading it never stalls cleanup.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_stderr_is_read_as_it_arrives_and_never_stalls_cleanup.py -q`.
