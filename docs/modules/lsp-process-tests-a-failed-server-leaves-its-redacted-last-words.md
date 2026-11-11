# Tests of `scripts/lsp_process.py`

- `tests/test_a_failed_server_leaves_its_redacted_last_words.py`: What a language server printed before it failed reaches its failure evidence.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_failed_server_leaves_its_redacted_last_words.py -q`.
