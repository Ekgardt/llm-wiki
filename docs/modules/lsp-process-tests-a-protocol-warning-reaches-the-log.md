# Tests of `scripts/lsp_process.py`

- `tests/test_a_protocol_warning_reaches_the_log.py`: A language-server protocol warning reaches stderr, not a callback nobody set.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_protocol_warning_reaches_the_log.py -q`.
