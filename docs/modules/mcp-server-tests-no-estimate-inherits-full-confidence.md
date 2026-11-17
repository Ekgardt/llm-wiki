# Tests of `scripts/mcp_server.py`

- `tests/test_no_estimate_inherits_full_confidence.py`: An estimate never inherits the envelope's full confidence (audit 2026-09-26 C-10).

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_no_estimate_inherits_full_confidence.py -q`.
