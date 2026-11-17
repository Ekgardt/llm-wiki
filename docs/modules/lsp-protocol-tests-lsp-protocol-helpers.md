# Tests of `scripts/lsp_protocol.py`

- `tests/lsp_protocol_helpers.py`: Helpers shared by test_lsp_protocol.py and the test files importing them.
- `tests/test_lsp_protocol_outcomes.py`: How the LSP transport settles a response, a cancellation, a deadline and a fatal error.

`scripts/lsp_protocol.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_protocol_outcomes.py -q`.
