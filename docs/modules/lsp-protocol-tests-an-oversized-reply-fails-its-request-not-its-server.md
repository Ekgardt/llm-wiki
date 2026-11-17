# Tests of `scripts/lsp_protocol.py`

- `tests/test_an_oversized_reply_fails_its_request_not_its_server.py`: An oversized reply fails its own request; the connection keeps answering (audit C-38).

`scripts/lsp_protocol.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_oversized_reply_fails_its_request_not_its_server.py -q`.
