# Tests of `scripts/lsp_process.py`

- `tests/test_a_stderr_tail_is_redacted_before_it_is_cut.py`: A failed server's stderr tail keeps no credential a cut or a line split hid (audit C-37).

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_stderr_tail_is_redacted_before_it_is_cut.py -q`.
